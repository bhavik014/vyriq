"""
Vyriq.ai — Full Production Flask SaaS
======================================
Complete backend with:
  • User authentication (signup / login / logout)
  • Stripe subscriptions & webhooks (real payments)
  • Plan enforcement (Starter 30/mo, Creator & Agency unlimited)
  • Admin panel (see all users, override plans)
  • SendGrid email (welcome, payment confirmation, weekly brief)
  • PostgreSQL (production) + SQLite (local dev) auto-switch
  • AI post analysis engine (OpenAI)
"""

import os
import json
import stripe
import hashlib
import hmac
from datetime import datetime, date
from functools import wraps

from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, flash, abort, session)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI
from dotenv import load_dotenv
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

load_dotenv()

# ════════════════════════════════════════════════════════════════════
#  App & DB Setup
# ════════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-me')

# Auto-switch: PostgreSQL on Railway, SQLite locally
_db_url = os.getenv('DATABASE_URL', '')
if _db_url.startswith('postgres://'):          # Railway gives postgres:// — fix it
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url or 'sqlite:///vyriq.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db          = SQLAlchemy(app)
login_mgr   = LoginManager(app)
login_mgr.login_view = 'login'

# ════════════════════════════════════════════════════════════════════
#  Stripe Setup
# ════════════════════════════════════════════════════════════════════
stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')

STRIPE_PUBLISHABLE_KEY  = os.getenv('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET   = os.getenv('STRIPE_WEBHOOK_SECRET', '')
STRIPE_CREATOR_PRICE_ID = os.getenv('STRIPE_CREATOR_PRICE_ID', '')
STRIPE_AGENCY_PRICE_ID  = os.getenv('STRIPE_AGENCY_PRICE_ID', '')
APP_URL                 = os.getenv('APP_URL', 'http://localhost:5000')

# ════════════════════════════════════════════════════════════════════
#  OpenAI Setup  (lazy-loaded to avoid httpx version conflicts)
# ════════════════════════════════════════════════════════════════════
_openai_client = None
AI_MODEL       = 'gpt-4o-mini'


def get_openai_client():
    """Return a cached OpenAI client, initializing it on first use.

    Deferring construction until the first request avoids the
    ``TypeError: Client.__init__() got an unexpected keyword argument
    'proxies'`` crash that occurs when openai==1.30.1 is paired with
    httpx>=0.28.0 and the module is imported at startup.
    """
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY', ''))
    return _openai_client

# ════════════════════════════════════════════════════════════════════
#  Plan Limits
# ════════════════════════════════════════════════════════════════════
PLAN_LIMITS = {
    'starter': 30,     # analyses per month
    'creator': 999999,
    'agency':  999999,
}

PLAN_NAMES = {
    'starter': 'Starter',
    'creator': 'Creator',
    'agency':  'Agency',
}


# ════════════════════════════════════════════════════════════════════
#  Database Models
# ════════════════════════════════════════════════════════════════════
class User(UserMixin, db.Model):
    """A Vyriq user account."""
    __tablename__ = 'users'

    id                      = db.Column(db.Integer, primary_key=True)
    email                   = db.Column(db.String(120), unique=True, nullable=False)
    password                = db.Column(db.String(200), nullable=False)
    name                    = db.Column(db.String(100), nullable=False)
    plan                    = db.Column(db.String(20), default='starter')
    stripe_customer_id      = db.Column(db.String(60), nullable=True)
    stripe_subscription_id  = db.Column(db.String(60), nullable=True)
    analysis_count          = db.Column(db.Integer, default=0)
    analysis_reset_date     = db.Column(db.Date, default=date.today)
    created_at              = db.Column(db.DateTime, default=datetime.utcnow)
    posts                   = db.relationship('Post', backref='user', lazy=True)

    @property
    def monthly_limit(self):
        return PLAN_LIMITS.get(self.plan, 30)

    @property
    def analyses_remaining(self):
        self._maybe_reset_counter()
        return max(0, self.monthly_limit - self.analysis_count)

    @property
    def is_at_limit(self):
        self._maybe_reset_counter()
        return self.analysis_count >= self.monthly_limit

    def _maybe_reset_counter(self):
        """Reset monthly counter on the 1st of a new month."""
        today = date.today()
        if self.analysis_reset_date is None or (
            today.month != self.analysis_reset_date.month or
            today.year  != self.analysis_reset_date.year
        ):
            self.analysis_count      = 0
            self.analysis_reset_date = today
            db.session.commit()

    @property
    def initials(self):
        parts = self.name.split()
        return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else '')).upper()


class Post(db.Model):
    """A social post analyzed by Vyriq's AI."""
    __tablename__ = 'posts'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    content    = db.Column(db.Text, nullable=False)
    platform   = db.Column(db.String(30), default='Instagram')
    hook_score = db.Column(db.Integer, default=0)
    summary    = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SocialConnection(db.Model):
    """Stores tokens for connected social accounts."""
    __tablename__ = 'social_connections'

    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    platform    = db.Column(db.String(30), nullable=False)  # 'instagram', 'facebook', etc.
    account_id  = db.Column(db.String(100))
    token       = db.Column(db.Text)
    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


@login_mgr.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ════════════════════════════════════════════════════════════════════
#  Email Helpers (SendGrid)
# ════════════════════════════════════════════════════════════════════
def send_email(to_email, to_name, subject, html_content):
    """Send a transactional email via SendGrid. Fails silently in dev."""
    api_key = os.getenv('SENDGRID_API_KEY', '')
    if not api_key:
        app.logger.info(f'[Email skipped — no SENDGRID_API_KEY] To: {to_email} | {subject}')
        return

    from_email = os.getenv('FROM_EMAIL', 'hello@vyriq.ai')
    from_name  = os.getenv('FROM_NAME',  'Vyriq')

    message = Mail(
        from_email=(from_email, from_name),
        to_emails=(to_email, to_name),
        subject=subject,
        html_content=html_content,
    )
    try:
        sg = SendGridAPIClient(api_key)
        sg.send(message)
    except Exception as e:
        app.logger.error(f'[SendGrid Error] {e}')


def send_welcome_email(user):
    html = f"""
    <div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:40px 20px;background:#0C0B09;color:#fff">
      <h1 style="font-family:Georgia,serif;color:#C4963A;margin-bottom:8px">Welcome to Vyriq, {user.name.split()[0]}.</h1>
      <p style="color:rgba(255,255,255,.6);line-height:1.7">Your account is live. Head to your dashboard to run your first AI post analysis — it takes 10 seconds.</p>
      <a href="{APP_URL}/dashboard" style="display:inline-block;margin-top:24px;padding:14px 28px;background:#C4963A;color:#0C0B09;text-decoration:none;font-weight:600;border-radius:4px;text-transform:uppercase;letter-spacing:.04em">Open Dashboard →</a>
      <p style="margin-top:32px;font-size:.8rem;color:rgba(255,255,255,.25)">You're on the <strong style="color:#C4963A">Starter plan</strong> (30 free analyses/month). Upgrade anytime from your dashboard.</p>
    </div>
    """
    send_email(user.email, user.name, 'Welcome to Vyriq ✦', html)


def send_upgrade_email(user, plan_name):
    html = f"""
    <div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:40px 20px;background:#0C0B09;color:#fff">
      <h1 style="font-family:Georgia,serif;color:#C4963A;margin-bottom:8px">You're on the {plan_name} plan.</h1>
      <p style="color:rgba(255,255,255,.6);line-height:1.7">Your upgrade is active. You now have unlimited post analyses and full access to every Vyriq feature.</p>
      <a href="{APP_URL}/dashboard" style="display:inline-block;margin-top:24px;padding:14px 28px;background:#C4963A;color:#0C0B09;text-decoration:none;font-weight:600;border-radius:4px;text-transform:uppercase;letter-spacing:.04em">Open Dashboard →</a>
      <p style="margin-top:32px;font-size:.8rem;color:rgba(255,255,255,.25)">Manage or cancel your subscription anytime from your dashboard → Billing.</p>
    </div>
    """
    send_email(user.email, user.name, f'Vyriq {plan_name} — You\'re in ✦', html)


# ════════════════════════════════════════════════════════════════════
#  Admin Auth Decorator
# ════════════════════════════════════════════════════════════════════
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        admin_pwd = os.getenv('ADMIN_PASSWORD', 'admin')
        if session.get('is_admin') != hashlib.sha256(admin_pwd.encode()).hexdigest():
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


# ════════════════════════════════════════════════════════════════════
#  Public Routes
# ════════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return render_template('index.html',
                           stripe_pk=STRIPE_PUBLISHABLE_KEY)


@app.route('/pricing')
def pricing():
    return redirect(url_for('index') + '#pricing')


# ════════════════════════════════════════════════════════════════════
#  Auth Routes
# ════════════════════════════════════════════════════════════════════
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not name or not email or not password:
            flash('Please fill in all fields.', 'error')
            return render_template('signup.html')

        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return render_template('signup.html')

        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.', 'error')
            return render_template('signup.html')

        user = User(
            name=name,
            email=email,
            password=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        send_welcome_email(user)
        flash(f'Welcome to Vyriq, {name.split()[0]}!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user     = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(request.args.get('next') or url_for('dashboard'))

        flash('Invalid email or password.', 'error')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# ════════════════════════════════════════════════════════════════════
#  Dashboard
# ════════════════════════════════════════════════════════════════════
@app.route('/dashboard')
@login_required
def dashboard():
    """Main user workspace."""
    posts = Post.query.filter_by(user_id=current_user.id)\
                      .order_by(Post.created_at.desc())\
                      .limit(20).all()

    # Dashboard navigation
    nav_links = [
        {'id': 'overview',  'icon': '📊', 'label': 'Overview'},
        {'id': 'analyzer',  'icon': '✦', 'label': 'AI Analyzer'},
        {'id': 'history',   'icon': '📋', 'label': 'Post History'},
        {'id': 'connect',   'icon': '🔗', 'label': 'Connections'},
        {'id': 'billing',   'icon': '💳', 'label': 'Billing'},
    ]

    return render_template('dashboard.html',
                           posts=posts,
                           plan_names=PLAN_NAMES,
                           nav_links=nav_links)


# ════════════════════════════════════════════════════════════════════
#  AI Analysis API
# ════════════════════════════════════════════════════════════════════
@app.route('/api/analyze', methods=['POST'])
@login_required
def analyze():
    # ── Plan enforcement ──────────────────────────────────────────
    if current_user.is_at_limit:
        return jsonify({
            'error': 'limit_reached',
            'message': (
                f"You've used all {current_user.monthly_limit} analyses this month on the "
                f"{PLAN_NAMES[current_user.plan]} plan. Upgrade to Creator for unlimited access."
            ),
            'upgrade_url': url_for('create_checkout_session', plan='creator', _external=True),
        }), 402

    data    = request.get_json()
    content = (data or {}).get('content', '').strip()

    if len(content) < 10:
        return jsonify({'error': 'too_short', 'message': 'Post too short to analyze (min 10 chars).'}), 400

    SYSTEM_PROMPT = """
You are Vyriq's content intelligence engine. You analyze social media post captions.

Your job:
1. Score the hook (first line) from 0–100 based on:
   - Curiosity gap (does it make you want to keep reading?)
   - Specificity (numbers and concrete claims score higher)
   - Emotional resonance
   - Scroll-stopping power

2. Write a 2-sentence honest summary of what works and what doesn't.

3. Give exactly 3 specific, actionable improvements.

Scoring guide:
- 90–100: Exceptional. Would stop 70%+ of scrollers.
- 75–89:  Strong. Minor improvements possible.
- 60–74:  Decent. Lacks a clear scroll-stopper.
- 40–59:  Weak. Bland or missing a specific angle.
- 0–39:   Poor. Needs a full rewrite of the opening.

ALWAYS respond in this exact JSON format (nothing else):
{
  "hookScore": <integer 0-100>,
  "summary": "<2 sentence analysis>",
  "suggestions": ["<suggestion 1>", "<suggestion 2>", "<suggestion 3>"]
}
"""

    try:
        response = get_openai_client().chat.completions.create(
            model=AI_MODEL,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user',   'content': f'Analyze this post:\n\n"{content}"'},
            ],
            temperature=0.7,
            max_tokens=500,
            response_format={'type': 'json_object'},
        )

        result = json.loads(response.choices[0].message.content or '{}')

        # Save to DB & increment counter
        post = Post(
            user_id    = current_user.id,
            content    = content[:500],
            hook_score = result.get('hookScore', 0),
            summary    = result.get('summary', ''),
        )
        db.session.add(post)
        current_user.analysis_count += 1
        db.session.commit()

        result['analyses_remaining'] = current_user.analyses_remaining
        result['plan']               = current_user.plan
        return jsonify(result)

    except Exception as e:
        app.logger.error(f'[Vyriq AI Error] {e}')
        return jsonify({
            'error': 'ai_error',
            'hookScore': 0,
            'summary': 'Analysis failed. Make sure OPENAI_API_KEY is set.',
            'suggestions': [
                'Check your OPENAI_API_KEY in the .env file.',
                'Get your key at platform.openai.com',
                'Restart the app after saving .env changes.',
            ]
        })


@app.route('/api/usage')
@login_required
def api_usage():
    """Returns the current user's plan + usage stats."""
    current_user._maybe_reset_counter()
    return jsonify({
        'plan':                current_user.plan,
        'plan_name':           PLAN_NAMES[current_user.plan],
        'analysis_count':      current_user.analysis_count,
        'monthly_limit':       current_user.monthly_limit,
        'analyses_remaining':  current_user.analyses_remaining,
        'is_at_limit':         current_user.is_at_limit,
    })


# ════════════════════════════════════════════════════════════════════
#  Stripe — Checkout Session
# ════════════════════════════════════════════════════════════════════
@app.route('/create-checkout-session', methods=['POST', 'GET'])
@login_required
def create_checkout_session():
    plan = request.args.get('plan') or request.form.get('plan', 'creator')

    price_id = (
        STRIPE_CREATOR_PRICE_ID if plan == 'creator' else STRIPE_AGENCY_PRICE_ID
    )

    if not price_id:
        flash('Stripe is not configured yet. Add STRIPE_CREATOR_PRICE_ID to .env', 'error')
        return redirect(url_for('dashboard'))

    try:
        # Reuse existing Stripe customer if available
        customer_id = current_user.stripe_customer_id or None

        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            customer_email=None if customer_id else current_user.email,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            subscription_data={
                'trial_period_days': 14,          # 14-day free trial
                'metadata': {
                    'user_id': current_user.id,
                    'plan':    plan,
                },
            },
            metadata={
                'user_id': current_user.id,
                'plan':    plan,
            },
            success_url=f'{APP_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{APP_URL}/payment/cancel',
            allow_promotion_codes=True,
        )
        return redirect(checkout.url, code=303)

    except stripe.error.StripeError as e:
        app.logger.error(f'[Stripe Error] {e}')
        flash('Something went wrong with the payment. Please try again.', 'error')
        return redirect(url_for('dashboard'))


# ════════════════════════════════════════════════════════════════════
#  Stripe — Webhook  (Stripe calls THIS when a payment happens)
# ════════════════════════════════════════════════════════════════════
@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload    = request.data
    sig_header = request.headers.get('Stripe-Signature', '')

    # Verify the request actually came from Stripe
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError:
            app.logger.warning('[Webhook] Invalid signature — rejected.')
            return jsonify({'error': 'invalid signature'}), 400
    else:
        # Dev mode — no verification
        event = stripe.Event.construct_from(
            json.loads(payload), stripe.api_key
        )

    # ── Handle events ─────────────────────────────────────────────
    if event['type'] == 'checkout.session.completed':
        session_obj = event['data']['object']
        _handle_checkout_complete(session_obj)

    elif event['type'] in ('customer.subscription.deleted',
                           'customer.subscription.paused'):
        sub = event['data']['object']
        _handle_subscription_cancelled(sub)

    elif event['type'] == 'customer.subscription.updated':
        sub = event['data']['object']
        _handle_subscription_updated(sub)

    elif event['type'] == 'invoice.payment_failed':
        invoice = event['data']['object']
        _handle_payment_failed(invoice)

    return jsonify({'status': 'ok'})


def _handle_checkout_complete(session_obj):
    """Upgrade the user's plan after successful checkout."""
    user_id = session_obj.get('metadata', {}).get('user_id')
    plan    = session_obj.get('metadata', {}).get('plan', 'creator')

    if not user_id:
        app.logger.error('[Webhook] No user_id in session metadata')
        return

    user = db.session.get(User, int(user_id))
    if not user:
        return

    user.plan                   = plan
    user.stripe_customer_id     = session_obj.get('customer')
    user.stripe_subscription_id = session_obj.get('subscription')
    db.session.commit()

    send_upgrade_email(user, PLAN_NAMES.get(plan, plan.capitalize()))
    app.logger.info(f'[Webhook] User {user.email} upgraded to {plan}')


def _handle_subscription_cancelled(sub):
    """Downgrade user to Starter when subscription is cancelled."""
    customer_id = sub.get('customer')
    user = User.query.filter_by(stripe_customer_id=customer_id).first()
    if user:
        user.plan                   = 'starter'
        user.stripe_subscription_id = None
        db.session.commit()
        app.logger.info(f'[Webhook] User {user.email} downgraded to starter')


def _handle_subscription_updated(sub):
    """Handle plan changes (e.g., Creator → Agency upgrade)."""
    customer_id = sub.get('customer')
    user = User.query.filter_by(stripe_customer_id=customer_id).first()
    if not user:
        return

    # Check which price the subscription now uses
    items = sub.get('items', {}).get('data', [])
    if items:
        price_id = items[0].get('price', {}).get('id')
        if price_id == STRIPE_CREATOR_PRICE_ID:
            user.plan = 'creator'
        elif price_id == STRIPE_AGENCY_PRICE_ID:
            user.plan = 'agency'
        db.session.commit()


def _handle_payment_failed(invoice):
    """Log payment failures (you can add email notification here)."""
    customer_id = invoice.get('customer')
    app.logger.warning(f'[Webhook] Payment failed for customer {customer_id}')


# ════════════════════════════════════════════════════════════════════
#  Stripe — Billing Portal  (user manages their own subscription)
# ════════════════════════════════════════════════════════════════════
@app.route('/billing')
@login_required
def billing():
    if not current_user.stripe_customer_id:
        flash("You don't have an active subscription to manage.", 'error')
        return redirect(url_for('dashboard'))

    try:
        portal = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=f'{APP_URL}/dashboard',
        )
        return redirect(portal.url)
    except stripe.error.StripeError as e:
        app.logger.error(f'[Stripe Billing Portal] {e}')
        flash('Could not open billing portal. Please try again.', 'error')
        return redirect(url_for('dashboard'))


# ════════════════════════════════════════════════════════════════════
#  Payment Outcome Pages
# ════════════════════════════════════════════════════════════════════
@app.route('/payment/success')
@login_required
def payment_success():
    session_id = request.args.get('session_id', '')
    # Optimistic: plan may already be updated via webhook
    return render_template('payment_success.html', session_id=session_id)


@app.route('/payment/cancel')
@login_required
def payment_cancel():
    return render_template('payment_cancel.html')


# ════════════════════════════════════════════════════════════════════
#  Admin Panel
# ════════════════════════════════════════════════════════════════════
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        pwd       = request.form.get('password', '')
        admin_pwd = os.getenv('ADMIN_PASSWORD', 'admin')
        if pwd == admin_pwd:
            session['is_admin'] = hashlib.sha256(admin_pwd.encode()).hexdigest()
            return redirect(url_for('admin_panel'))
        flash('Wrong password.', 'error')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('index'))


@app.route('/admin')
@admin_required
def admin_panel():
    users       = User.query.order_by(User.created_at.desc()).all()
    total_posts = Post.query.count()
    plan_counts = {
        'starter': User.query.filter_by(plan='starter').count(),
        'creator': User.query.filter_by(plan='creator').count(),
        'agency':  User.query.filter_by(plan='agency').count(),
    }
    # Rough MRR estimate
    mrr = plan_counts['creator'] * 29 + plan_counts['agency'] * 79

    return render_template('admin.html',
                           users=users,
                           total_posts=total_posts,
                           plan_counts=plan_counts,
                           mrr=mrr,
                           plan_names=PLAN_NAMES)


@app.route('/admin/set-plan', methods=['POST'])
@admin_required
def admin_set_plan():
    user_id  = request.form.get('user_id')
    new_plan = request.form.get('plan')

    if not user_id or new_plan not in ('starter', 'creator', 'agency'):
        flash('Invalid request.', 'error')
        return redirect(url_for('admin_panel'))

    user = db.session.get(User, int(user_id))
    if user:
        user.plan = new_plan
        db.session.commit()
        flash(f'{user.email} → {new_plan}', 'success')

    return redirect(url_for('admin_panel'))


@app.route('/admin/delete-user', methods=['POST'])
@admin_required
def admin_delete_user():
    user_id = request.form.get('user_id')
    user    = db.session.get(User, int(user_id)) if user_id else None
    if user:
        Post.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
        flash(f'Deleted {user.email}', 'success')
    return redirect(url_for('admin_panel'))


# ════════════════════════════════════════════════════════════════════
#  Weekly Email Brief  (APScheduler — runs every Monday 8 AM UTC)
# ════════════════════════════════════════════════════════════════════
def send_weekly_briefs():
    """Sends a weekly AI brief email to all paid users."""
    with app.app_context():
        paid_users = User.query.filter(User.plan.in_(['creator', 'agency'])).all()
        for user in paid_users:
            posts = Post.query.filter_by(user_id=user.id)\
                              .order_by(Post.created_at.desc())\
                              .limit(5).all()

            if not posts:
                continue

            avg_score = round(sum(p.hook_score for p in posts) / len(posts))
            best      = max(posts, key=lambda p: p.hook_score)

            html = f"""
            <div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:40px 20px;background:#0C0B09;color:#fff">
              <div style="color:#C4963A;font-size:.65rem;letter-spacing:.2em;text-transform:uppercase;margin-bottom:16px">— Weekly Brief —</div>
              <h1 style="font-family:Georgia,serif;font-size:2rem;margin-bottom:8px;color:#fff">Your Vyriq Recap</h1>
              <p style="color:rgba(255,255,255,.5);font-size:.9rem;margin-bottom:32px">Here's what the AI found in your content this week.</p>

              <div style="background:rgba(196,150,58,.1);border:1px solid rgba(196,150,58,.2);border-radius:12px;padding:20px;margin-bottom:20px">
                <div style="font-size:.65rem;color:#C4963A;letter-spacing:.15em;text-transform:uppercase;margin-bottom:8px">Average Hook Score</div>
                <div style="font-size:2.5rem;color:#fff;font-family:Georgia,serif">{avg_score}<span style="font-size:1rem;color:rgba(255,255,255,.4)">/100</span></div>
              </div>

              <div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:20px;margin-bottom:24px">
                <div style="font-size:.65rem;color:#C4963A;letter-spacing:.15em;text-transform:uppercase;margin-bottom:8px">Best Hook This Week ({best.hook_score}/100)</div>
                <p style="font-size:.88rem;color:rgba(255,255,255,.6);font-style:italic;line-height:1.6">"{best.content[:120]}..."</p>
              </div>

              <p style="color:rgba(255,255,255,.5);font-size:.85rem;line-height:1.7">
                <strong style="color:#fff">This week's focus:</strong> Analyze 3 new posts and aim for a hook score above {min(avg_score + 10, 95)}.
              </p>

              <a href="{APP_URL}/dashboard" style="display:inline-block;margin-top:24px;padding:14px 28px;background:#C4963A;color:#0C0B09;text-decoration:none;font-weight:600;border-radius:4px;text-transform:uppercase;font-size:.8rem;letter-spacing:.04em">Open Dashboard →</a>
            </div>
            """
            send_email(user.email, user.name, '📊 Your Vyriq Weekly Brief', html)

        app.logger.info(f'[Weekly Brief] Sent to {len(paid_users)} users')


def start_scheduler():
    """Start the background scheduler for weekly email briefs."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            send_weekly_briefs,
            trigger='cron',
            day_of_week='mon',
            hour=8,
            minute=0,
        )
        scheduler.start()
        app.logger.info('[Scheduler] Weekly brief job scheduled (Mon 8AM UTC)')
    except Exception as e:
        app.logger.error(f'[Scheduler] Failed to start: {e}')


# ════════════════════════════════════════════════════════════════════
#  Startup
# ════════════════════════════════════════════════════════════════════

# Init DB — runs on every startup (safe: only creates missing tables)
with app.app_context():
    db.create_all()

# Start the weekly email scheduler
start_scheduler()


if __name__ == '__main__':
    print('\n✅ Vyriq is running!')
    print('   Open: http://localhost:5000')
    print('   Admin: http://localhost:5000/admin\n')
    app.run(debug=True, port=5000)
