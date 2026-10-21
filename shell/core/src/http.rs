use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use crate::error::CoreError;

/// 生图回的 base64 图可能有几十 MiB；上限只是防御"对端没完没了"。
pub const MAX_BODY_BYTES: usize = 512 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct Response {
    pub status: u16,
    pub headers: BTreeMap<String, String>,
    pub body: Vec<u8>,
}

impl Response {
    pub fn text(&self) -> Result<&str, CoreError> {
        std::str::from_utf8(&self.body)
            .map_err(|e| CoreError::Protocol(format!("响应不是 UTF-8: {e}")))
    }

    pub fn json(&self) -> Result<serde_json::Value, CoreError> {
        serde_json::from_slice(&self.body)
            .map_err(|e| CoreError::Protocol(format!("响应不是 JSON: {e}")))
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(&name.to_ascii_lowercase()).map(|s| s.as_str())
    }

    fn is_success(&self) -> bool {
        (200..300).contains(&self.status)
    }
}

pub struct FormFile<'a> {
    pub field: &'a str,
    pub filename: &'a str,
    pub content_type: &'a str,
    pub bytes: &'a [u8],
}

fn quote_escape(value: &str) -> String {
    value
        .chars()
        .filter(|c| *c != '\r' && *c != '\n' && *c != '"')
        .collect()
}

pub fn multipart_body(
    boundary: &str,
    files: &[FormFile<'_>],
    fields: &BTreeMap<String, String>,
) -> Vec<u8> {
    let mut out = Vec::new();
    for (name, value) in fields {
        out.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        out.extend_from_slice(
            format!(
                "Content-Disposition: form-data; name=\"{}\"\r\n\r\n",
                quote_escape(name)
            )
            .as_bytes(),
        );
        out.extend_from_slice(value.as_bytes());
        out.extend_from_slice(b"\r\n");
    }
    for f in files {
        out.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        out.extend_from_slice(
            format!(
                "Content-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n",
                quote_escape(f.field),
                quote_escape(f.filename)
            )
            .as_bytes(),
        );
        out.extend_from_slice(format!("Content-Type: {}\r\n\r\n", f.content_type).as_bytes());
        out.extend_from_slice(f.bytes);
        out.extend_from_slice(b"\r\n");
    }
    out.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());
    out
}

pub fn new_boundary() -> Result<String, CoreError> {
    Ok(format!("----local-ai-studio-{}", crate::token_hex(16)?))
}

/// 外壳只连自己的回环端口；这里不做重定向跟随，避免被上游带去别的地址。
pub fn request(
    host: &str,
    port: u16,
    method: &str,
    path: &str,
    headers: &[(String, String)],
    body: Option<&[u8]>,
    timeout: Duration,
) -> Result<Response, CoreError> {
    request_capped(host, port, method, path, headers, body, timeout, MAX_BODY_BYTES)
}

#[allow(clippy::too_many_arguments)]
fn request_capped(
    host: &str,
    port: u16,
    method: &str,
    path: &str,
    headers: &[(String, String)],
    body: Option<&[u8]>,
    timeout: Duration,
    max_body: usize,
) -> Result<Response, CoreError> {
    if !path.starts_with('/') {
        return Err(CoreError::Protocol(format!("请求路径 {path:?} 不以 / 开头")));
    }
    let addr = (host, port)
        .to_socket_addrs()
        .map_err(|e| CoreError::Protocol(format!("解析 {host}:{port} 失败: {e}")))?
        .next()
        .ok_or_else(|| CoreError::Protocol(format!("{host}:{port} 没有地址")))?;
    let mut stream = TcpStream::connect_timeout(&addr, timeout)
        .map_err(|e| CoreError::Protocol(format!("连不上 {host}:{port}: {e}")))?;
    stream
        .set_read_timeout(Some(timeout))
        .and_then(|_| stream.set_write_timeout(Some(timeout)))
        .map_err(|e| CoreError::Protocol(format!("设置超时失败: {e}")))?;

    let mut head = format!("{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\n");
    head.push_str("Connection: close\r\n");
    for (name, value) in headers {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    if let Some(bytes) = body {
        head.push_str(&format!("Content-Length: {}\r\n", bytes.len()));
    }
    head.push_str("\r\n");
    stream
        .write_all(head.as_bytes())
        .and_then(|_| match body {
            Some(bytes) => stream.write_all(bytes),
            None => Ok(()),
        })
        .and_then(|_| stream.flush())
        .map_err(|e| CoreError::Protocol(format!("发请求失败: {e}")))?;

    let mut conn = Conn::new(stream);
    let status_line = conn.read_line()?;
    let status_text = String::from_utf8_lossy(&status_line);
    let mut parts = status_text.trim_end().splitn(3, ' ');
    let version = parts.next().unwrap_or("");
    if !version.starts_with("HTTP/") {
        return Err(CoreError::Protocol(format!(
            "响应状态行不是 HTTP: {status_text:?}"
        )));
    }
    let status: u16 = parts
        .next()
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| CoreError::Protocol(format!("状态行没有状态码: {status_text:?}")))?;

    let mut headers_map = BTreeMap::new();
    loop {
        let line = conn.read_line()?;
        if line == b"\r\n" || line == b"\n" || line.is_empty() {
            break;
        }
        let text = String::from_utf8_lossy(&line);
        let (name, value) = text.split_once(':').ok_or_else(|| {
            CoreError::Protocol(format!("响应头没有冒号: {:?}", text.trim_end()))
        })?;
        headers_map.insert(name.trim().to_ascii_lowercase(), value.trim().to_string());
    }

    let chunked = headers_map
        .get("transfer-encoding")
        .map(|v| v.to_ascii_lowercase().contains("chunked"))
        .unwrap_or(false);
    let body = if chunked {
        conn.read_chunked(max_body)?
    } else if let Some(len) = headers_map.get("content-length") {
        let n: usize = len
            .parse()
            .map_err(|_| CoreError::Protocol(format!("Content-Length {len:?} 不是数字")))?;
        if n > max_body {
            return Err(CoreError::Protocol(format!(
                "响应体 {n} 字节超过上限 {max_body}"
            )));
        }
        conn.read_exact(n)?
    } else {
        conn.read_to_end(max_body)?
    };

    Ok(Response {
        status,
        headers: headers_map,
        body,
    })
}

struct Conn {
    stream: TcpStream,
    buf: Vec<u8>,
    pos: usize,
}

impl Conn {
    fn new(stream: TcpStream) -> Self {
        Self {
            stream,
            buf: Vec::new(),
            pos: 0,
        }
    }

    /// 只把网卡上新到的一块接在未消费的数据后面。
    fn fill(&mut self) -> Result<usize, CoreError> {
        if self.pos > 0 {
            self.buf.drain(..self.pos);
            self.pos = 0;
        }
        let mut chunk = [0u8; 65536];
        let n = self
            .stream
            .read(&mut chunk)
            .map_err(|e| CoreError::Protocol(format!("读响应失败: {e}")))?;
        self.buf.extend_from_slice(&chunk[..n]);
        Ok(n)
    }

    fn read_line(&mut self) -> Result<Vec<u8>, CoreError> {
        loop {
            if let Some(idx) = self.buf[self.pos..].iter().position(|b| *b == b'\n') {
                let end = self.pos + idx + 1;
                let line = self.buf[self.pos..end].to_vec();
                self.pos = end;
                return Ok(line);
            }
            if self.fill()? == 0 {
                return Err(CoreError::Protocol("响应在状态行/头部中途断开".into()));
            }
        }
    }

    fn read_exact(&mut self, n: usize) -> Result<Vec<u8>, CoreError> {
        while self.buf.len() - self.pos < n {
            if self.fill()? == 0 {
                return Err(CoreError::Protocol(format!(
                    "响应体少 {} 字节就断了",
                    n - (self.buf.len() - self.pos)
                )));
            }
        }
        let out = self.buf[self.pos..self.pos + n].to_vec();
        self.pos += n;
        Ok(out)
    }

    fn read_to_end(&mut self, max_body: usize) -> Result<Vec<u8>, CoreError> {
        loop {
            if self.buf.len() - self.pos > max_body {
                return Err(CoreError::Protocol(format!(
                    "响应体超过上限 {max_body} 字节"
                )));
            }
            if self.fill()? == 0 {
                let out = self.buf[self.pos..].to_vec();
                self.pos = self.buf.len();
                return Ok(out);
            }
        }
    }

    fn read_chunked(&mut self, max_body: usize) -> Result<Vec<u8>, CoreError> {
        let mut out: Vec<u8> = Vec::new();
        loop {
            let line = self.read_line()?;
            let text = String::from_utf8_lossy(&line);
            let size_text = text.trim_end().split(';').next().unwrap_or("").trim();
            let size = usize::from_str_radix(size_text, 16).map_err(|_| {
                CoreError::Protocol(format!("chunk 大小 {:?} 不是十六进制", size_text))
            })?;
            if size == 0 {
                loop {
                    let trailer = self.read_line()?;
                    if trailer == b"\r\n" || trailer == b"\n" {
                        break;
                    }
                }
                return Ok(out);
            }
            if out.len() + size > max_body {
                return Err(CoreError::Protocol(format!(
                    "chunked 响应体超过上限 {max_body} 字节"
                )));
            }
            let chunk = self.read_exact(size)?;
            out.extend_from_slice(&chunk);
            let crlf = self.read_exact(2)?;
            if crlf != b"\r\n" {
                return Err(CoreError::Protocol("chunk 后面不是 CRLF".into()));
            }
        }
    }
}

/// 外壳自己发的 HTTP：非 2xx 一律把状态码带出来，别把错误页当结果。
pub fn expect_2xx(response: Response, what: &str) -> Result<Response, CoreError> {
    if response.is_success() {
        Ok(response)
    } else {
        let body = String::from_utf8_lossy(&response.body);
        let snippet: String = body.chars().take(200).collect();
        Err(CoreError::Protocol(format!(
            "{what}: 上游回 {} —— {snippet}",
            response.status
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    fn serve_once(response: Vec<u8>) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            if let Ok((mut s, _)) = listener.accept() {
                let mut scratch = [0u8; 4096];
                let _ = s.read(&mut scratch);
                let _ = s.write_all(&response);
            }
        });
        port
    }

    fn serve_raw(parts: &[&[u8]]) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let parts: Vec<Vec<u8>> = parts.iter().map(|p| p.to_vec()).collect();
        thread::spawn(move || {
            if let Ok((mut s, _)) = listener.accept() {
                let mut scratch = [0u8; 4096];
                let _ = s.read(&mut scratch);
                for p in parts {
                    let _ = s.write_all(&p);
                }
            }
        });
        port
    }

    fn get(port: u16) -> Response {
        request(
            "127.0.0.1",
            port,
            "GET",
            "/health",
            &[],
            None,
            Duration::from_secs(5),
        )
        .unwrap()
    }

    #[test]
    fn content_length_body_and_headers_are_read() {
        let port = serve_once(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhello"
                .to_vec(),
        );
        let r = get(port);
        assert_eq!(r.status, 200);
        assert_eq!(r.text().unwrap(), "hello");
        assert_eq!(r.header("content-type"), Some("text/plain"));
        assert_eq!(r.header("Content-Type"), Some("text/plain"));
    }

    #[test]
    fn chunked_bodies_are_decoded_and_trailers_ignored() {
        let port = serve_once(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n\
              5\r\nhello\r\n1\r\n!\r\n0\r\nX-Trailer: yes\r\n\r\n"
                .to_vec(),
        );
        assert_eq!(get(port).text().unwrap(), "hello!");
    }

    #[test]
    fn connection_close_without_length_reads_to_eof() {
        let port = serve_once(b"HTTP/1.1 200 OK\r\n\r\nuntil-close".to_vec());
        assert_eq!(get(port).text().unwrap(), "until-close");
    }

    #[test]
    fn a_404_keeps_its_status_and_expect_2xx_reports_it() {
        let port = serve_once(b"HTTP/1.1 404 Not Found\r\nContent-Length: 3\r\n\r\nno!".to_vec());
        let r = get(port);
        assert_eq!(r.status, 404);
        let err = expect_2xx(r, "探活").unwrap_err().to_string();
        assert!(err.contains("404"), "{err}");
        assert!(err.contains("no!"), "{err}");
    }

    #[test]
    fn a_short_content_length_body_is_an_error_not_a_silent_truncation() {
        let port = serve_once(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc".to_vec());
        let err = request(
            "127.0.0.1",
            port,
            "GET",
            "/",
            &[],
            None,
            Duration::from_secs(2),
        )
        .unwrap_err()
        .to_string();
        assert!(err.contains("7"), "{err}");
    }

    #[test]
    fn the_body_cap_trips() {
        let port = serve_once(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\n123456789".to_vec());
        let err = request_capped(
            "127.0.0.1",
            port,
            "GET",
            "/",
            &[],
            None,
            Duration::from_secs(2),
            8,
        )
        .unwrap_err()
        .to_string();
        assert!(err.contains("上限"), "{err}");
    }

    #[test]
    fn request_body_is_sent_with_a_content_length() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, rx) = std::sync::mpsc::channel();
        thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 8192];
            let n = s.read(&mut buf).unwrap();
            tx.send(String::from_utf8_lossy(&buf[..n]).into_owned()).unwrap();
            let _ = s.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok");
        });
        let r = request(
            "127.0.0.1",
            port,
            "POST",
            "/v1/chat/completions",
            &[("Content-Type".into(), "application/json".into())],
            Some(b"{\"a\":1}"),
            Duration::from_secs(5),
        )
        .unwrap();
        assert_eq!(r.text().unwrap(), "ok");
        let seen = rx.recv().unwrap();
        assert!(seen.starts_with("POST /v1/chat/completions HTTP/1.1\r\n"), "{seen}");
        assert!(seen.contains("Content-Length: 7\r\n"), "{seen}");
        assert!(seen.ends_with("{\"a\":1}"), "{seen}");
    }

    #[test]
    fn multipart_carries_the_file_field_and_extra_fields() {
        let mut fields = BTreeMap::new();
        fields.insert("response_format".to_string(), "srt".to_string());
        let body = multipart_body(
            "BOUND",
            &[FormFile {
                field: "file",
                filename: "a.wav",
                content_type: "audio/wav",
                bytes: b"RIFF",
            }],
            &fields,
        );
        let text = String::from_utf8(body).unwrap();
        assert!(text.contains("name=\"response_format\"\r\n\r\nsrt\r\n"), "{text}");
        assert!(
            text.contains("name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\nRIFF"),
            "{text}"
        );
        assert!(text.ends_with("--BOUND--\r\n"), "{text}");
    }

    #[test]
    fn filenames_cannot_break_the_multipart_header() {
        let body = multipart_body(
            "B",
            &[FormFile {
                field: "file",
                filename: "a\"b\r\nX: y.wav",
                content_type: "audio/wav",
                bytes: b"x",
            }],
            &BTreeMap::new(),
        );
        let text = String::from_utf8(body).unwrap();
        assert!(text.contains("filename=\"abX: y.wav\"") || text.contains("filename=\"a bX: y.wav\""), "{text}");
        assert!(!text.contains("\r\nX: y.wav"), "{text}");
    }

    #[test]
    fn a_non_http_status_line_is_refused() {
        let port = serve_once(b"SSH-2.0-OpenSSH\r\n\r\n".to_vec());
        assert!(request(
            "127.0.0.1",
            port,
            "GET",
            "/",
            &[],
            None,
            Duration::from_secs(2)
        )
        .is_err());
    }

    #[test]
    fn responses_split_across_packets_are_reassembled() {
        let port = serve_raw(&[
            b"HTTP/1.1 200 OK\r\nContent-Le",
            b"ngth: 5\r\n\r\nhe",
            b"llo",
        ]);
        assert_eq!(get(port).text().unwrap(), "hello");
    }
}
