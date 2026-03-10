use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use image::ImageReader;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Cursor;
use std::path::Path;
use std::time::UNIX_EPOCH;

const MAX_DIMENSION: u32 = 200;
const SVG_MAX_SIZE: u64 = 100_000; // 100KB

#[derive(Debug, Serialize)]
pub struct ThumbnailResult {
    pub data_url: String,
    pub width: u32,
    pub height: u32,
}

fn cache_dir() -> Result<std::path::PathBuf, String> {
    let base = dirs::home_dir().ok_or("Cannot determine home directory")?;
    let dir = base.join(".pocketpaw").join("cache").join("thumbnails");
    if !dir.exists() {
        fs::create_dir_all(&dir)
            .map_err(|e| format!("Failed to create cache dir: {}", e))?;
    }
    Ok(dir)
}

fn cache_key(path: &str, modified: u64) -> String {
    let mut hasher = Sha256::new();
    hasher.update(format!("{}:{}", path, modified));
    let hash = hasher.finalize();
    // Use first 8 bytes → 16 hex chars
    hash[..8].iter().map(|b| format!("{:02x}", b)).collect()
}

fn get_modified_time(path: &Path) -> u64 {
    fs::metadata(path)
        .ok()
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

#[tauri::command]
pub async fn fs_thumbnail(path: String) -> Result<ThumbnailResult, String> {
    let file_path = Path::new(&path);

    if !file_path.exists() {
        return Err(format!("File not found: {}", path));
    }

    let ext = file_path
        .extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .unwrap_or_default();

    let modified = get_modified_time(file_path);

    // SVG special case: return raw SVG as base64 if under size limit
    if ext == "svg" {
        let meta = fs::metadata(file_path)
            .map_err(|e| format!("Failed to stat: {}", e))?;
        if meta.len() <= SVG_MAX_SIZE {
            let data = fs::read(file_path)
                .map_err(|e| format!("Failed to read SVG: {}", e))?;
            let b64 = BASE64.encode(&data);
            return Ok(ThumbnailResult {
                data_url: format!("data:image/svg+xml;base64,{}", b64),
                width: 0, // SVG dimensions are dynamic
                height: 0,
            });
        }
        return Err("SVG file too large for thumbnail".to_string());
    }

    // Check disk cache
    let cache_base = cache_dir()?;
    let key = cache_key(&path, modified);
    let cache_path = cache_base.join(format!("{}.jpg", key));

    if cache_path.exists() {
        // Cache hit — read and return
        let cached = fs::read(&cache_path)
            .map_err(|e| format!("Failed to read cache: {}", e))?;

        // Decode just to get dimensions
        let img = ImageReader::new(Cursor::new(&cached))
            .with_guessed_format()
            .map_err(|e| format!("Failed to read cached image: {}", e))?
            .decode()
            .map_err(|e| format!("Failed to decode cached image: {}", e))?;

        let b64 = BASE64.encode(&cached);
        return Ok(ThumbnailResult {
            data_url: format!("data:image/jpeg;base64,{}", b64),
            width: img.width(),
            height: img.height(),
        });
    }

    // Cache miss — decode, resize, cache, return
    let data = fs::read(file_path)
        .map_err(|e| format!("Failed to read image: {}", e))?;

    let img = ImageReader::new(Cursor::new(&data))
        .with_guessed_format()
        .map_err(|e| format!("Failed to detect format: {}", e))?
        .decode()
        .map_err(|e| format!("Failed to decode image: {}", e))?;

    let thumb = img.thumbnail(MAX_DIMENSION, MAX_DIMENSION);
    let (w, h) = (thumb.width(), thumb.height());

    // Encode as JPEG
    let mut jpeg_buf = Cursor::new(Vec::new());
    thumb
        .write_to(&mut jpeg_buf, image::ImageFormat::Jpeg)
        .map_err(|e| format!("Failed to encode JPEG: {}", e))?;
    let jpeg_bytes = jpeg_buf.into_inner();

    // Write to disk cache (best effort)
    let _ = fs::write(&cache_path, &jpeg_bytes);

    let b64 = BASE64.encode(&jpeg_bytes);
    Ok(ThumbnailResult {
        data_url: format!("data:image/jpeg;base64,{}", b64),
        width: w,
        height: h,
    })
}
