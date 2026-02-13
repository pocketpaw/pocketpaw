"""Web server for QR code pairing flow.

Changes:
  - 2026-02-03: Optimised deep link flow. Now fetches bot username and listens for /start <secret>.
  - 2026-02-03: Added automatic port finding when default port is busy.
"""

import asyncio
import base64
import logging
import secrets
import socket
from io import BytesIO
from typing import Optional

try:
    import qrcode
    import qrcode.image.svg
    import uvicorn
    from fastapi import FastAPI, Form, Request
    from fastapi.responses import HTMLResponse
except ImportError as _exc:
    raise ImportError(
        "Web server dependencies (fastapi, uvicorn, qrcode) are required "
        "but not installed. Install them with: pip install 'pocketpaw[dashboard]'"
    ) from _exc

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError as _exc:
    raise ImportError(
        "'python-telegram-bot' is required for the pairing flow. "
        "Install it with: pip install 'pocketpaw[telegram]'"
    ) from _exc

from pocketclaw.config import Settings


def find_available_port(start_port: int, max_attempts: int = 10) -> int:
    """Find an available port starting from start_port.

    Tries start_port first, then increments until finding an available one.
    """
    for offset in range(max_attempts):
        port = start_port + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise OSError(
        f"Could not find available port in range {start_port}-{start_port + max_attempts}"
    )


logger = logging.getLogger(__name__)

# Global state for pairing
_pairing_complete = asyncio.Event()
_session_secret: Optional[str] = None
_settings: Optional[Settings] = None
_temp_bot_app: Optional[Application] = None


def generate_qr_svg(deep_link: str) -> str:
    """Generate QR code as SVG string."""
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(deep_link)
    qr.make(fit=True)

    # Generate as PNG and convert to base64
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    img_base64 = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{img_base64}"


async def _handle_pairing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start <secret> during pairing."""
    global _settings, _pairing_complete

    if not update.message or not update.effective_user:
        return

    text = update.message.text
    if not text:
        return

    # Check payload
    # Format: /start <secret>
    # Note: Telegram sends "/start" or "/start <payload>"
    parts = text.split()

    if len(parts) < 2:
        await update.message.reply_text(
            "⏳ Waiting for pairing... Please scan the QR code to start."
        )
        return

    secret = parts[1]

    if secret != _session_secret:
        await update.message.reply_text("❌ Invalid session token. Please refresh the setup page.")
        return

    # Success!
    user_id = update.effective_user.id
    username = update.effective_user.username

    if _settings:
        _settings.allowed_user_id = user_id
        _settings.save()

    logger.info(f"✅ Paired with user: {username} ({user_id})")

    await update.message.reply_text(
        "🎉 **Connected!**\n\nPocketPaw is now paired with this device.\nYou can close the browser window now.",
        parse_mode="Markdown",
    )

    _pairing_complete.set()


def create_app(settings: Settings) -> FastAPI:
    """Create the FastAPI app for pairing."""
    global _session_secret, _settings
    _settings = settings
    _session_secret = secrets.token_urlsafe(32)

    app = FastAPI(title="PocketPaw Setup")

    @app.get("/", response_class=HTMLResponse)
    async def setup_page():
        """Render the setup page."""
        return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PocketPaw Setup</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
        }}
        .container {{
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(10px);
            border-radius: 24px;
            padding: 48px;
            max-width: 480px;
            width: 90%;
            text-align: center;
            border: 1px solid rgba(255,255,255,0.1);
        }}
        .logo {{ font-size: 64px; margin-bottom: 16px; }}
        h1 {{ font-size: 28px; margin-bottom: 8px; }}
        .tagline {{ color: #888; margin-bottom: 32px; }}
        .step {{ 
            background: rgba(255,255,255,0.05); 
            border-radius: 12px; 
            padding: 20px; 
            margin-bottom: 16px;
            text-align: left;
        }}
        .step-number {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            width: 28px; height: 28px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 14px;
            margin-right: 12px;
        }}
        input {{
            width: 100%;
            padding: 14px 16px;
            border: 1px solid rgba(255,255,255,0.2);
            border-radius: 8px;
            background: rgba(0,0,0,0.3);
            color: #fff;
            font-size: 14px;
            margin-top: 12px;
        }}
        input:focus {{ outline: none; border-color: #667eea; }}
        button {{
            width: 100%;
            padding: 16px;
            border: none;
            border-radius: 12px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #fff;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 24px;
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        button:hover {{ transform: translateY(-2px); box-shadow: 0 8px 24px rgba(102,126,234,0.4); }}
        .qr-section {{ display: none; margin-top: 32px; }}
        .qr-section.active {{ display: block; }}
        .qr-code {{ 
            background: #fff; 
            padding: 16px; 
            border-radius: 16px; 
            display: inline-block;
            margin: 16px 0;
        }}
        .qr-code img {{ width: 200px; height: 200px; }}
        .success {{ 
            background: rgba(34, 197, 94, 0.2); 
            border: 1px solid rgba(34, 197, 94, 0.5);
            padding: 16px;
            border-radius: 12px;
            margin-top: 16px;
            animation: fadeIn 0.5s ease-out;
        }}
        @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        .api-keys {{
            margin-top: 16px;
            text-align: left;
        }}
        .api-keys label {{
            display: block;
            font-size: 12px;
            color: #888;
            margin-top: 12px;
            margin-bottom: 4px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">🦀</div>
        <h1>PocketPaw Setup</h1>
        <p class="tagline">Your AI agent, on your machine</p>
        
        <form id="setup-form" method="POST" action="/setup">
            <div class="step">
                <span class="step-number">1</span>
                <strong>Create a Telegram Bot</strong>
                <p style="color: #888; font-size: 14px; margin-top: 8px;">
                    Open <a href="https://t.me/BotFather" target="_blank" style="color: #667eea;">@BotFather</a> 
                    on Telegram and send <code>/newbot</code>
                </p>
                <input type="text" name="bot_token" placeholder="Paste your bot token here..." required>
            </div>
            
            <div class="step api-keys">
                <span class="step-number">2</span>
                <strong>LLM API Keys (Optional)</strong>
                <p style="color: #888; font-size: 14px; margin-top: 8px;">
                    Add API keys for cloud LLMs. Leave blank to use local Ollama only.
                </p>
                <label>OpenAI API Key</label>
                <input type="password" name="openai_key" placeholder="sk-...">
                <label>Anthropic API Key</label>
                <input type="password" name="anthropic_key" placeholder="sk-ant-...">
            </div>
            
            <button type="submit">Generate QR Code →</button>
        </form>
        
        <div id="qr-section" class="qr-section">
            <div class="step">
                <span class="step-number">3</span>
                <strong>Scan with Telegram</strong>
                <p style="color: #888; font-size: 14px; margin-top: 8px;">
                    Open your phone camera and scan this QR code
                </p>
            </div>
            <div class="qr-code">
                <img id="qr-image" src="" alt="QR Code">
            </div>
            <p style="color: #888; font-size: 14px;">Waiting for connection...</p>
        </div>
        
        <div id="success-section" class="success" style="display: none;">
            ✅ <strong>Connected!</strong> PocketPaw is now running.
        </div>
    </div>
    
    <script>
        document.getElementById('setup-form').addEventListener('submit', async (e) => {{
            e.preventDefault();
            const btn = e.target.querySelector('button');
            const originalText = btn.innerText;
            btn.innerText = "Connecting...";
            btn.disabled = true;
            
            const formData = new FormData(e.target);
            try {{
                const response = await fetch('/setup', {{
                    method: 'POST',
                    body: formData
                }});
                const data = await response.json();
                if (data.error) {{
                    alert(data.error);
                    btn.innerText = originalText;
                    btn.disabled = false;
                    return;
                }}
                if (data.qr_url) {{
                    document.getElementById('qr-image').src = data.qr_url;
                    document.getElementById('qr-section').classList.add('active');
                    e.target.style.display = 'none';
                    pollStatus();
                }}
            }} catch (err) {{
                alert("Failed to connect. Please check your internet.");
                btn.innerText = originalText;
                btn.disabled = false;
            }}
        }});
        
        async function pollStatus() {{
            while (true) {{
                try {{
                    const response = await fetch('/status');
                    const data = await response.json();
                    if (data.paired) {{
                        document.getElementById('qr-section').style.display = 'none';
                        document.getElementById('success-section').style.display = 'block';
                        setTimeout(() => window.close(), 3000);
                        break;
                    }}
                }} catch (e) {{ console.error(e); }}
                await new Promise(r => setTimeout(r, 1000));
            }}
        }}
    </script>
</body>
</html>
"""

    @app.post("/setup")
    async def setup(
        bot_token: str = Form(...),
        openai_key: Optional[str] = Form(None),
        anthropic_key: Optional[str] = Form(None),
    ):
        """Handle setup form submission."""
        global _settings, _temp_bot_app

        # Save the bot token
        _settings.telegram_bot_token = bot_token
        if openai_key:
            _settings.openai_api_key = openai_key
        if anthropic_key:
            _settings.anthropic_api_key = anthropic_key

        try:
            # 1. Initialize temporary bot
            builder = Application.builder().token(bot_token)
            app = builder.build()

            # 2. Verify token and get username
            bot_user = await app.bot.get_me()
            username = bot_user.username

            # 3. Generate Deep Link
            # Format: https://t.me/<username>?start=<secret>
            deep_link = f"https://t.me/{username}?start={_session_secret}"
            qr_data = generate_qr_svg(deep_link)

            # 4. Start Listening for /start <secret>
            app.add_handler(CommandHandler("start", _handle_pairing_start))

            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            _temp_bot_app = app

            return {"qr_url": qr_data, "session_secret": _session_secret}

        except Exception as e:
            logger.error(f"Setup failed: {e}")
            return {"error": f"Failed to connect to Telegram: {str(e)}"}

    @app.get("/status")
    async def status():
        """Check pairing status."""
        return {"paired": _pairing_complete.is_set()}

    @app.post("/complete")
    async def complete(user_id: int):
        """Called internally when pairing is complete."""
        global _settings
        _settings.allowed_user_id = user_id
        _settings.save()
        _pairing_complete.set()
        return {"ok": True}

    return app


async def run_pairing_server(settings: Settings) -> int:
    """Run the pairing server until pairing is complete.

    Returns the port that was used (may differ from settings if port was busy).
    """
    app = create_app(settings)

    # Find available port (handles "address already in use" error)
    try:
        port = find_available_port(settings.web_port)
    except OSError as e:
        logger.error(f"Could not find available port: {e}")
        raise

    if port != settings.web_port:
        logger.info(f"Port {settings.web_port} busy, using port {port} instead")

    config = uvicorn.Config(app, host=settings.web_host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    # Run server in background
    server_task = asyncio.create_task(server.serve())

    try:
        # Wait for pairing to complete
        await _pairing_complete.wait()

        # Give a moment for the success response to return
        await asyncio.sleep(1)

    finally:
        # Shutdown temporary bot
        global _temp_bot_app
        if _temp_bot_app:
            if _temp_bot_app.updater.running:
                await _temp_bot_app.updater.stop()
            if _temp_bot_app.running:
                await _temp_bot_app.stop()
            await _temp_bot_app.shutdown()

        # Shutdown web server
        server.should_exit = True
        await server_task

    return port
