# Vyriq — Flask SaaS Starter

## 🚀 Quick Start (3 steps)

### Step 1 — Install Python
Download from: https://python.org (choose Python 3.11+)
Check ✅ "Add Python to PATH" during install.

### Step 2 — Install & Run
Open PowerShell in this folder and run:

```bash
pip install -r requirements.txt
python app.py
```

### Step 3 — Open your browser
Go to: http://localhost:5000

---

## 📁 Project Structure
```
Vyriq/
├── app.py                  ← Main Flask app (start here)
├── requirements.txt        ← Python packages to install
├── .env.example            ← Copy as .env and fill in keys
├── templates/
│   ├── base.html           ← Shared layout (navbar, fonts)
│   ├── index.html          ← Landing page
│   └── dashboard.html      ← Dashboard (after login)
└── static/
    └── css/
        └── styles.css      ← All your styles
```

## 🔑 Environment Variables
Copy `.env.example` → `.env` and fill in:
- `OPENAI_API_KEY` — from platform.openai.com
- `SECRET_KEY` — any random string

## 💰 Monthly Cost at Zero Users: $0
Everything runs free until you have paying customers.
