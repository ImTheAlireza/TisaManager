<?php
/**
 * Bale API bridge — upload this single file to an Iran-side web host.
 *
 * Why: tapi.bale.ai refuses TCP connections from non-Iranian IPs, so a bot
 * server hosted abroad cannot reach it directly. This bridge receives the
 * bot's API calls over HTTPS and re-sends them from inside Iran.
 *
 * Security model:
 *   - Every request must carry X-Bridge-Key matching BRIDGE_KEY (timing-safe
 *     compare). No key -> 403.
 *   - The upstream host is hard-coded (tapi.bale.ai): this is not an open
 *     proxy. The API method is whitelisted to plain letters, the bot token
 *     to Telegram/Bale token characters.
 *   - Bot tokens travel in request HEADERS (X-Bale-Token), not the URL, so
 *     they never land in the web server's access logs. Bodies are never
 *     logged.
 *
 * Setup on the Iran-side host:
 *   1) Set BRIDGE_KEY below to a long random string (e.g. `openssl rand -hex 32`).
 *   2) Upload to a private-looking path, e.g. /fp9x2/bale_bridge.php.
 *   3) On the bot server, set in .env:
 *        BALE_API_BASE=https://YOUR-SITE.ir/fp9x2/bale_bridge.php
 *        BALE_BRIDGE_KEY=<the same key>
 *   4) If you upload big videos, raise upload_max_filesize / post_max_size
 *      (cPanel: MultiPHP INI Editor) — the bridge accepts up to 50 MB.
 */

declare(strict_types=1);

const UPSTREAM_BASE   = 'https://tapi.bale.ai';
const BRIDGE_KEY      = 'REPLACE_WITH_LONG_RANDOM_STRING'; // openssl rand -hex 32
const MAX_UPLOAD      = 52428800; // 50 MB
const CURL_TIMEOUT    = 300;      // long: large media uploads
const CURL_CONNECT_TO = 15;

header('Content-Type: application/json');
header('X-Bridge: bale-bridge/1');

function fail(int $code, string $message): void {
    http_response_code($code);
    echo json_encode(['ok' => false, 'description' => $message]);
    exit;
}

/* --- 1) Auth ------------------------------------------------------------- */
$key = $_SERVER['HTTP_X_BRIDGE_KEY'] ?? '';
if ($key === '' || !is_string($key) || !hash_equals(BRIDGE_KEY, $key)) {
    fail(403, 'forbidden');
}

/* --- 2) Shape of the request --------------------------------------------- */
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    fail(405, 'POST only');
}

$token  = $_SERVER['HTTP_X_BALE_TOKEN']  ?? '';
$method = $_SERVER['HTTP_X_BALE_METHOD'] ?? '';
if (!is_string($token)  || !preg_match('/^[0-9A-Za-z:_\-]{20,60}$/', $token)) {
    fail(400, 'bad token header');
}
if (!is_string($method) || !preg_match('/^[A-Za-z]{2,32}$/', $method)) {
    fail(400, 'bad method header');
}

/* --- 3) Rebuild the upstream request -------------------------------------- */
// Field files arrive as a normal multipart upload and are forwarded as
// CURLFile parts; scalar fields pass through untouched.
$post = [];
foreach ($_POST as $k => $v) {
    if (is_string($v) || is_numeric($v)) {
        $post[(string)$k] = (string)$v;
    }
}
foreach ($_FILES as $field => $f) {
    $err = (int)($f['error'] ?? UPLOAD_ERR_NO_FILE);
    if ($err !== UPLOAD_ERR_OK) {
        fail(400, "upload field {$field} failed (code {$err})");
    }
    $size = (int)($f['size'] ?? 0);
    if ($size > MAX_UPLOAD) {
        fail(413, "field {$field} exceeds " . (int)(MAX_UPLOAD / 1048576) . ' MB limit');
    }
    $post[(string)$field] = new CURLFile(
        (string)($f['tmp_name'] ?? ''),
        (string)($f['type'] ?? '') ?: 'application/octet-stream',
        (string)($f['name'] ?? '') !== '' ? (string)$f['name'] : (string)$field
    );
}

/* --- 4) Forward from inside Iran ------------------------------------------ */
@set_time_limit(0);

$ch = curl_init();
curl_setopt_array($ch, [
    CURLOPT_URL            => UPSTREAM_BASE . '/bot' . $token . '/' . $method,
    CURLOPT_POST           => true,
    CURLOPT_POSTFIELDS     => $post ?: new stdClass(), // {} avoids "array to string" on empty
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT        => CURL_TIMEOUT,
    CURLOPT_CONNECTTIMEOUT => CURL_CONNECT_TO,
    CURLOPT_FOLLOWLOCATION => false,
    CURLOPT_SSL_VERIFYPEER => true,   // Bale's real certificate, verified here
    CURLOPT_SSL_VERIFYHOST => 2,
    CURLOPT_HTTPHEADER     => ['Expect:'], // skip the 100-continue round trip
]);

$body   = curl_exec($ch);
$status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
$err    = curl_error($ch); // never contains the URL/token
curl_close($ch);

if ($body === false) {
    fail(502, 'bridge upstream error: ' . $err);
}

http_response_code($status ?: 502);
echo $body; // verbatim Bale API JSON
