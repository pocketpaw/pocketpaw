use serde::{Deserialize, Serialize};
use std::fs;
#[cfg(desktop)]
use std::io::{BufRead, BufReader, Write};
#[cfg(desktop)]
use std::net::TcpListener;
use std::time::Duration;
#[cfg(desktop)]
use tauri::{AppHandle, Emitter};

const TOKEN_FILE: &str = "client_oauth.json";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OAuthTokens {
    pub access_token: String,
    pub refresh_token: Option<String>,
    pub expires_at: u64,
    pub scopes: Vec<String>,
}

fn token_file_path() -> Result<std::path::PathBuf, String> {
    let home = dirs::home_dir().ok_or("Could not determine home directory")?;
    Ok(home.join(".pocketpaw").join(TOKEN_FILE))
}

#[tauri::command]
pub fn read_oauth_tokens() -> Result<OAuthTokens, String> {
    let path = token_file_path()?;
    let data = fs::read_to_string(&path).map_err(|e| format!("Failed to read tokens: {}", e))?;
    serde_json::from_str(&data).map_err(|e| format!("Failed to parse tokens: {}", e))
}

#[tauri::command]
pub fn save_oauth_tokens(tokens: OAuthTokens) -> Result<(), String> {
    let path = token_file_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("Failed to create dir: {}", e))?;
    }
    let data =
        serde_json::to_string_pretty(&tokens).map_err(|e| format!("Failed to serialize: {}", e))?;
    fs::write(&path, data).map_err(|e| format!("Failed to write tokens: {}", e))?;

    // Restrict file permissions to owner-only (0600) on Unix so other users
    // on the system cannot read the plaintext OAuth tokens.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let perms = fs::Permissions::from_mode(0o600);
        fs::set_permissions(&path, perms)
            .map_err(|e| format!("Failed to set token file permissions: {}", e))?;
    }

    Ok(())
}

#[tauri::command]
pub fn clear_oauth_tokens() -> Result<(), String> {
    let path = token_file_path()?;
    if path.exists() {
        fs::remove_file(&path).map_err(|e| format!("Failed to delete tokens: {}", e))?;
    }
    Ok(())
}

/// Starts a temporary localhost HTTP server on a random port.
/// Returns the port number immediately. A background thread accepts one
/// connection, extracts the OAuth redirect URL, emits an `oauth-redirect`
/// Tauri event, and sends back a "close this tab" HTML page.
#[cfg(desktop)]
#[tauri::command]
pub fn start_oauth_server(app: AppHandle) -> Result<u16, String> {
    let listener =
        TcpListener::bind("127.0.0.1:0").map_err(|e| format!("Failed to bind: {}", e))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("Failed to get port: {}", e))?
        .port();

    std::thread::spawn(move || {
        // Use non-blocking mode with a poll loop so the thread exits after a
        // timeout instead of blocking forever. This prevents thread leaks when
        // the user cancels the OAuth flow and reopens the dialog.
        let _ = listener.set_nonblocking(true);
        let deadline = std::time::Instant::now() + Duration::from_secs(300); // 5 minutes

        let stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    if std::time::Instant::now() >= deadline {
                        return; // timed out, exit the thread cleanly
                    }
                    std::thread::sleep(Duration::from_millis(200));
                    continue;
                }
                Err(_) => return, // unexpected error, exit the thread
            }
        };

        let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
        let mut reader = BufReader::new(&stream);
        let mut request_line = String::new();
        if reader.read_line(&mut request_line).is_ok() {
            // Parse: "GET /path?query HTTP/1.1"
            let parts: Vec<&str> = request_line.split_whitespace().collect();
            if parts.len() >= 2 {
                let path = parts[1]; // e.g. "/?code=abc&state=xyz" or "/oauth-callback?..."
                let redirect_url = format!("http://localhost:{}{}", port, path);

                // Emit the redirect URL to the frontend
                let _ = app.emit("oauth-redirect", redirect_url);
            }

            // Send back a simple HTML response
            let body = r#"<!DOCTYPE html>
<html>
<head><title>PocketPaw</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f9fafb;color:#374151;">
<div style="text-align:center;">
<h2 style="margin:0 0 8px;">Sign-in complete!</h2>
<p style="color:#6b7280;">You can close this tab and return to PocketPaw.</p>
</div>
</body>
</html>"#;

            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );

            let mut writer = stream;
            let _ = writer.write_all(response.as_bytes());
            let _ = writer.flush();
        }
    });

    Ok(port)
}

/// Validate that a proxy URL targets localhost only (prevents SSRF).
/// Uses proper URL parsing to ensure the host is exactly localhost or 127.0.0.1,
/// preventing bypasses like `http://localhost.attacker.com`.
fn validate_proxy_url(input: &str) -> Result<(), String> {
    let parsed = url::Url::parse(input)
        .map_err(|e| format!("Invalid URL '{}': {}", input, e))?;

    if parsed.scheme() != "http" {
        return Err(format!(
            "Proxy only allows http scheme, got: {}",
            parsed.scheme()
        ));
    }

    match parsed.host_str() {
        Some("127.0.0.1") | Some("localhost") => Ok(()),
        Some(host) => Err(format!(
            "Proxy only allows requests to localhost/127.0.0.1, got host: {}",
            host
        )),
        None => Err("URL has no host".to_string()),
    }
}

/// Proxy an HTTP POST to the backend, bypassing CORS/mixed-content restrictions
/// in the Tauri webview. Returns the response body as a string.
/// Only allows requests to localhost to prevent SSRF.
#[tauri::command]
pub fn proxy_post(url: String, body: String) -> Result<String, String> {
    validate_proxy_url(&url)?;

    let agent = ureq::Agent::new_with_config(
        ureq::config::Config::builder()
            .timeout_global(Some(Duration::from_secs(10)))
            .build(),
    );
    let response = agent
        .post(&url)
        .content_type("application/json")
        .send(body.as_bytes())
        .map_err(|e| format!("Request failed: {}", e))?;

    response
        .into_body()
        .read_to_string()
        .map_err(|e| format!("Failed to read response: {}", e))
}

/// Proxy an HTTP GET to the backend, bypassing CORS/mixed-content restrictions.
/// Only allows requests to localhost to prevent SSRF.
#[tauri::command]
pub fn proxy_get(url: String) -> Result<String, String> {
    validate_proxy_url(&url)?;

    let agent = ureq::Agent::new_with_config(
        ureq::config::Config::builder()
            .timeout_global(Some(Duration::from_secs(10)))
            .build(),
    );
    let response = agent
        .get(&url)
        .call()
        .map_err(|e| format!("Request failed: {}", e))?;

    response
        .into_body()
        .read_to_string()
        .map_err(|e| format!("Failed to read response: {}", e))
}
