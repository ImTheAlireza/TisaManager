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
 *   3) RAISE PHP'S UPLOAD LIMITS — this is the step that decides whether
 *      albums work. A media group is ONE request carrying every file in it,
 *      and PHP throws away a body larger than post_max_size (and any single
 *      file larger than upload_max_filesize) *without a word*; this script
 *      then forwards an empty request to Bale, which answers "400 Bad
 *      Request" and the bot can neither deliver the post nor tell why.
 *      Put this in .user.ini next to the script (or edit php.ini / cPanel's
 *      MultiPHP INI Editor):
 *          post_max_size = 64M
 *          upload_max_filesize = 50M
 *          max_file_uploads = 20
 *      64M of post_max_size covers a 50 MB album (Bale's per-file ceiling)
 *      with room for the JSON and the multipart overhead.
 *   4) On the bot server, set in .env:
 *        BALE_API_BASE=https://YOUR-SITE.ir/fp9x2/bale_bridge.php
 *        BALE_BRIDGE_KEY=<the same key>
 *   5) Check what the host really enforces (these are the values the bridge
 *      compares against, and it names them in every 413):
 *        php -i | grep -E 'post_max_size|upload_max_filesize'
 */

declare(strict_types=1);

const UPSTREAM_BASE   = 'https://tapi.bale.ai';
const BRIDGE_KEY      = 'REPLACE_WITH_LONG_RANDOM_STRING'; // openssl rand -hex 32
const MAX_UPLOAD      = 52428800; // 50 MB
const CURL_TIMEOUT    = 300;      // long: large media uploads
const CURL_CONNECT_TO = 15;
// A body this big cannot be a field-less call (getMe is ~30 bytes), so
// "no fields at all" at this size means PHP threw the body away.
const DROPPED_BODY_MIN = 65536;

header('Content-Type: application/json');
header('X-Bridge: bale-bridge/1');

function fail(int $code, string $message): void {
    http_response_code($code);
    // error_code mirrors Bale's own error shape so the bot can read any
    // bridge failure exactly like an API failure.
    echo json_encode(['ok' => false, 'error_code' => $code, 'description' => $message]);
    exit;
}

/** "64M" / "52428800" / "0" (unlimited) -> bytes. */
function ini_bytes(string $value): int {
    $value = trim($value);
    if ($value === '') {
        return 0;
    }
    $number = (int)$value;
    switch (strtolower(substr($value, -1))) {
        case 'g': return $number * 1024 * 1024 * 1024;
        case 'm': return $number * 1024 * 1024;
        case 'k': return $number * 1024;
        default:  return $number; // already bytes ("0" = unlimited)
    }
}

/** Why PHP refused one file, in words instead of an error code. */
function upload_error_text(int $code): string {
    $reasons = [
        UPLOAD_ERR_INI_SIZE   => 'file is larger than upload_max_filesize',
        UPLOAD_ERR_FORM_SIZE  => 'file is larger than the MAX_FILE_SIZE form field',
        UPLOAD_ERR_PARTIAL    => 'file was only partially uploaded',
        UPLOAD_ERR_NO_FILE    => 'no file arrived in this field',
        UPLOAD_ERR_NO_TMP_DIR => 'the host has no temp folder for uploads',
        UPLOAD_ERR_CANT_WRITE => 'the host could not write the upload to disk',
        UPLOAD_ERR_EXTENSION  => 'a PHP extension stopped the upload',
    ];
    return $reasons[$code] ?? ('PHP upload error ' . $code);
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
// PHP drops oversized POSTs before this script can see them, so the only
// trace left is a Content-Length that contradicts what arrived. Check it
// first and name the limit: this is the difference between "the album never
// arrives" and "the host's post_max_size is 8M, the album is 41 MB".
$postMax        = (string)ini_get('post_max_size');
$uploadMax      = (string)ini_get('upload_max_filesize');
$postMaxBytes   = ini_bytes($postMax);
$contentLength  = (int)($_SERVER['CONTENT_LENGTH'] ?? 0);

if ($postMaxBytes > 0 && $contentLength > $postMaxBytes) {
    fail(413, sprintf(
        'request body of %.1f MB exceeds this host\'s post_max_size (%s). '
        . 'Raise post_max_size to 64M and upload_max_filesize to 50M on the bridge host, then retry.',
        $contentLength / 1048576,
        $postMax
    ));
}

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
        // A file over upload_max_filesize lands here with error code 1 and no
        // bytes; forwarding it would leave the media JSON pointing at an
        // attachment that does not exist, which Bale answers with a bare 400.
        $tooBig = ($err === UPLOAD_ERR_INI_SIZE || $err === UPLOAD_ERR_FORM_SIZE);
        fail($tooBig ? 413 : 400, sprintf(
            "upload field '%s' was rejected: %s (upload_max_filesize = %s, post_max_size = %s). "
            . 'Raise upload_max_filesize to 50M and post_max_size to 64M on the bridge host, then retry.',
            (string)$field,
            upload_error_text($err),
            $uploadMax,
            $postMax
        ));
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

if (!$post && $contentLength >= DROPPED_BODY_MIN) {
    // Same body, same cause, caught after parsing: PHP's post_max_size (or a
    // WAF in front of it) dropped everything, so there is nothing to forward.
    fail(413, sprintf(
        'PHP discarded the whole request body (%d KB) — post_max_size = %s, upload_max_filesize = %s. '
        . 'Raise post_max_size to 64M and upload_max_filesize to 50M on the bridge host, then retry.',
        intdiv($contentLength, 1024),
        $postMax,
        $uploadMax
    ));
}

/* --- 4) Forward from inside Iran ------------------------------------------ */
@set_time_limit(0);

$ch = curl_init();
curl_setopt_array($ch, [
    CURLOPT_URL            => UPSTREAM_BASE . '/bot' . $token . '/' . $method,
    CURLOPT_POST           => true,
    CURLOPT_POSTFIELDS     => $post ?: '', // empty string, not stdClass: cURL would try (string) cast
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
