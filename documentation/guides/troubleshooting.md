# 🛠 Troubleshooting Guide

This guide addresses common installation and runtime issues encountered while setting up and running PocketPaw.

---

## 1. Server Starts but Dashboard Is Not Accessible

If you see output like:

```text
🌐 Open http://127.0.0.1:8888 in your browser
```

But the dashboard does not load:

### ✅ Check the Following

1. Ensure you are visiting the exact URL shown in the terminal.
2. If running with:

```bash
pocketpaw --host 0.0.0.0
```

Access using:

```text
http://localhost:8888
```

or

```text
http://<your-local-ip>:8888
```

3. Confirm no firewall or antivirus is blocking the port.
4. If using Docker, ensure ports are mapped correctly:

```bash
docker run -p 8888:8888 pocketpaw
```

---

## 2. Address Already in Use

Error example:

```text
OSError: [Errno 98] Address already in use
```

This means another application is already using the selected port.

### ✅ Solution

Run on a different port:

```bash
pocketpaw --port 8890
```

Or find the process using the port:

**macOS / Linux**

```bash
lsof -i :8888
```

**Windows**

```powershell
netstat -ano | findstr :8888
```

Terminate the process or choose another port.

---

## 3. Missing Environment Variables

Error example:

```text
ValueError: Missing required environment variable: POCKETPAW_OPENAI_API_KEY
```

### ✅ Solution

Set required environment variables before running the application.

**Windows (CMD)**

```cmd
set POCKETPAW_OPENAI_API_KEY=your_key_here
```

**Windows (PowerShell)**

```powershell
$env:POCKETPAW_OPENAI_API_KEY="your_key_here"
```

**macOS / Linux**

```bash
export POCKETPAW_OPENAI_API_KEY=your_key_here
```

Restart the terminal after setting variables.

---

## 4. Headless Server Binding to 0.0.0.0

If you see:

```text
Headless server detected — binding to 0.0.0.0
```

This means the server is accessible on all network interfaces.

Access the app using:

```text
http://localhost:8888
```

or

```text
http://<your-machine-ip>:8888
```

If you want local-only access:

```bash
pocketpaw --host 127.0.0.1
```

---

## 5. Python Version Issues

PocketPaw requires **Python 3.11 or higher**.

### 🔎 Check Your Version

```bash
python --version
```

or

```bash
python3 --version
```

If your version is below 3.11, install Python 3.11+ from:

https://www.python.org/downloads/

### 🔄 Recreate Virtual Environment (If Dependencies Fail)

```bash
python -m venv .venv
```

**macOS / Linux**

```bash
source .venv/bin/activate
```

**Windows**

```powershell
.venv\Scripts\Activate.ps1
```

Then reinstall dependencies.

---

## 6. uv Command Not Found

Error:

```text
uv: command not found
```

### ✅ Official Installation (Recommended)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart your terminal and verify:

```bash
uv --version
```

### ⚠ Alternative (Works but Not Official)

```bash
pip install uv
```

If still not recognized, ensure your Python Scripts directory is added to your system `PATH`.

---

## 📌 Reporting Issues

If problems persist, open a GitHub issue and include:

- Operating system
- Python version (exact version, must be 3.11+)
- Exact command used to start PocketPaw
- Full error message (copy-paste preferred)
- Screenshot or terminal logs (if applicable)

Providing complete information helps maintainers reproduce and resolve the issue efficiently.
