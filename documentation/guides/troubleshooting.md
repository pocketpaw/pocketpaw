# Troubleshooting Guide

This guide addresses common installation and runtime issues encountered while setting up and running PocketPaw.

---

## 1. Server Starts but Dashboard Is Not Accessible

If you see output like:

🌐 Open http://127.0.0.1:8888 in your browser

But the dashboard does not load:

### Check the following:

1. Ensure you are visiting the exact URL shown in the terminal.
2. If running with:
   pocketpaw --host 0.0.0.0

   Access using:
   http://localhost:8888
   or
   http://<your-local-ip>:8888

3. Confirm no firewall or antivirus is blocking the port.
4. If using Docker, ensure ports are mapped correctly:
   docker run -p 8888:8888 pocketpaw

---

## 2. Address Already in Use

Error example:
OSError: [Errno 98] Address already in use

This means another application is already using the selected port.

### Solution:

Run on a different port:
pocketpaw --port 8890

Or find the process using the port:

macOS / Linux:
lsof -i :8888

Windows:
netstat -ano | findstr :8888

Terminate the process or choose another port.

---

## 3. Missing Environment Variables

Error example:
ValueError: Missing required environment variable: POCKETPAW_OPENAI_API_KEY

### Solution:

Set required environment variables before running the application.

Windows (CMD):
set POCKETPAW_OPENAI_API_KEY=your_key_here

Windows (PowerShell):
$env:POCKETPAW_OPENAI_API_KEY="your_key_here"

macOS / Linux:
export POCKETPAW_OPENAI_API_KEY=your_key_here

Restart the terminal after setting variables.

---

## 4. Headless Server Binding to 0.0.0.0

If you see:
Headless server detected — binding to 0.0.0.0

This means the server is accessible on all network interfaces.

Access the app using:
http://localhost:8888
or
http://<your-machine-ip>:8888

If you want local-only access:
pocketpaw --host 127.0.0.1

---

## 5. Python Version Issues

Check version:
python --version

Ensure Python 3.x is installed.

If dependencies fail to install, recreate your virtual environment:

python -m venv venv
source venv/bin/activate  (macOS/Linux)
venv\Scripts\activate     (Windows)

Then reinstall dependencies.

---

## 6. uv Command Not Found

Error:
uv: command not found

Install uv:
pip install uv

Verify:
uv --version

If still not recognized, ensure your Python Scripts directory is added to your system PATH.

---

## Reporting Issues

If problems persist, open a GitHub issue and include:

- Operating system
- Python version
- Exact command used to start PocketPaw
- Full error message
- Screenshot or terminal logs

Providing complete information helps maintainers reproduce and resolve the issue efficiently.
