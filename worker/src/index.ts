/**
 * tobisite-preview — раздача черновиков с *.tobisitepreview.com.
 *
 * Поддомен это префикс в R2: pravo-i-dilo.tobisitepreview.com/ отдаёт объект
 * pravo-i-dilo/index.html. Деплоя на превью нет — публикация это PUT в бакет
 * (tools/publish_r2.py).
 *
 * Заголовки безопасности ставятся на КАЖДЫЙ ответ, включая 404, статику и
 * ответы API: черновик чужой компании не должен попасть ни в индекс, ни в
 * архив. Поэтому в wrangler.toml включён run_worker_first — иначе статика
 * из [assets] уезжала бы клиенту мимо воркера и без этих заголовков.
 */

// Типы биндингов объявлены здесь, а не взяты из @cloudflare/workers-types:
// у воркера нет node_modules, wrangler собирает TS через esbuild без проверки
// типов. Имена нарочно свои, чтобы не столкнуться с официальными типами, если пакет
// однажды поставят.
interface R2Meta {
  size: number;
  httpEtag: string;
  writeHttpMetadata(headers: Headers): void;
}

interface R2Body extends R2Meta {
  body: ReadableStream;
}

interface PreviewBucket {
  get(key: string, options?: { onlyIf?: Headers }): Promise<R2Body | R2Meta | null>;
  head(key: string): Promise<R2Meta | null>;
}

interface AssetsFetcher {
  fetch(request: Request): Promise<Response>;
}

interface HitsDataset {
  writeDataPoint(point: { blobs?: string[]; doubles?: number[]; indexes?: string[] }): void;
}

export interface Env {
  PREVIEWS: PreviewBucket;
  ASSETS?: AssetsFetcher;
  HITS?: HitsDataset;
  TG_BOT_TOKEN?: string;
  TG_ADMIN_CHAT_ID?: string;
}

const CSP = [
  "default-src 'self'",
  "img-src 'self' data:",
  "style-src 'self' 'unsafe-inline'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
  "object-src 'none'",
].join("; ");

const SLUG_RE = /^[a-z0-9-]{1,63}$/;
const HIT_EVENTS = new Set(["view", "scroll50", "dwell20", "cta_click"]);
const MAX_BODY = 16 * 1024;

const NOT_FOUND_PAGE = `<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Page not found</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:20vh auto;max-width:30rem;padding:0 1.5rem;color:#333}</style>
<h1>Page not found</h1>
<p>This page is not available.</p>
`;

/** Один и тот же набор заголовков на любой ответ воркера. */
function secure(response: Response): Response {
  const headers = new Headers(response.headers);
  headers.set("X-Robots-Tag", "noindex, nofollow, noarchive");
  headers.set("Content-Security-Policy", CSP);
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "no-referrer");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function notFound(): Response {
  return new Response(NOT_FOUND_PAGE, {
    status: 404,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

// Лимит живёт в памяти изолята. Изолятов у Cloudflare много и они
// перезапускаются, так что честного глобального «5 в минуту» здесь нет: это
// заслон от простого флуда с одного адреса, а не гарантия. Глобальный счётчик
// потребовал бы Durable Object или KV — оба лишние на Free-плане.
const RATE_LIMIT = 5;
const RATE_WINDOW_MS = 60_000;
const recent = new Map<string, number[]>();

function rateLimited(ip: string): boolean {
  const now = Date.now();
  const stamps = (recent.get(ip) ?? []).filter((at) => now - at < RATE_WINDOW_MS);
  if (stamps.length >= RATE_LIMIT) {
    recent.set(ip, stamps);
    return true;
  }
  stamps.push(now);
  recent.set(ip, stamps);
  if (recent.size > 1000) {
    for (const [key, times] of recent) {
      if (times.every((at) => now - at >= RATE_WINDOW_MS)) recent.delete(key);
    }
  }
  return false;
}

/** Строка из формы: без управляющих символов, переводы строк оставляем. */
function text(value: unknown): string {
  if (typeof value !== "string") return "";
  return value
    .replace(/\r\n?/g, "\n")   // формы шлют CRLF
    .replace(/[\u0000-\u0009\u000b-\u001f\u007f]/g, " ")
    .trim();
}

async function readFields(request: Request): Promise<Record<string, string> | null> {
  if (Number(request.headers.get("content-length") ?? 0) > MAX_BODY) return null;
  const type = request.headers.get("content-type") ?? "";
  try {
    const raw: Record<string, unknown> = {};
    if (type.includes("application/json")) {
      Object.assign(raw, (await request.json()) as Record<string, unknown>);
    } else {
      for (const [key, value] of await request.formData()) raw[key] = value;
    }
    return {
      name: text(raw.name),
      phone: text(raw.phone),
      message: text(raw.message).slice(0, 2000),
      // honeypot: форма cta_form_short шлёт company_website; website — на всякий случай
      website: text(raw.company_website) || text(raw.website),
    };
  } catch {
    return null;
  }
}

async function sendToTelegram(
  env: Env,
  slug: string,
  host: string,
  lead: Record<string, string>,
): Promise<boolean> {
  if (!env.TG_BOT_TOKEN || !env.TG_ADMIN_CHAT_ID) {
    console.error("lead dropped: TG_BOT_TOKEN / TG_ADMIN_CHAT_ID не заданы");
    return false;
  }
  const lines = [
    `Заявка с превью: ${slug}`,
    host ? `Адрес: ${host}` : "",
    `Имя: ${lead.name}`,
    `Телефон: ${lead.phone}`,
    lead.message ? `Сообщение: ${lead.message}` : "",
  ].filter(Boolean);
  try {
    const response = await fetch(
      `https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          chat_id: env.TG_ADMIN_CHAT_ID,
          // без parse_mode: текст лида тогда не нужно экранировать
          text: lines.join("\n"),
          disable_web_page_preview: true,
        }),
      },
    );
    if (!response.ok) console.error("telegram", response.status, await response.text());
    return response.ok;
  } catch (err) {
    console.error("telegram", err);
    return false;
  }
}

async function handleLead(request: Request, env: Env, slug: string): Promise<Response> {
  if (request.method !== "POST") return json({ ok: false, error: "method" }, 405);

  const fields = await readFields(request);
  if (!fields) return json({ ok: false, error: "bad_request" }, 400);

  // honeypot: поле company_website в форме спрятано, человек его не заполняет.
  // Боту отвечаем успехом, чтобы он не искал обход.
  if (fields.website) return json({ ok: true });

  const { name, phone, message } = fields;
  if (name.length < 2 || name.length > 80 || phone.length < 5 || phone.length > 40) {
    return json({ ok: false, error: "invalid" }, 400);
  }

  // Проверка «реальная отправка заявки end-to-end» проходит ту же валидацию,
  // но не тревожит админ-чат и не тратит лимит запросов.
  if (request.headers.get("X-Tobisite-Test") === "1") return json({ ok: true, test: true });

  if (rateLimited(request.headers.get("CF-Connecting-IP") ?? "unknown")) {
    return json({ ok: false, error: "rate_limit" }, 429);
  }

  const host = request.headers.get("host") ?? "";
  const sent = await sendToTelegram(env, slug, host, { name, phone, message });
  return sent ? json({ ok: true }) : json({ ok: false, error: "delivery" }, 502);
}

async function handleHit(request: Request, env: Env, hostSlug: string): Promise<Response> {
  if (request.method !== "POST") return new Response(null, { status: 405 });

  let event = "";
  let slug = hostSlug;
  try {
    const raw = await request.text(); // sendBeacon по умолчанию шлёт text/plain
    if (raw.length > MAX_BODY) return new Response(null, { status: 413 });
    const data = JSON.parse(raw) as Record<string, unknown>;
    event = typeof data.event === "string" ? data.event : "";
    if (typeof data.slug === "string" && SLUG_RE.test(data.slug)) slug = data.slug;
  } catch {
    return new Response(null, { status: 400 });
  }
  if (!HIT_EVENTS.has(event)) return new Response(null, { status: 400 });

  // Ни куки, ни адреса: только слаг и тип события.
  // На аккаунте без Analytics Engine биндинга нет — тогда это тихий no-op.
  env.HITS?.writeDataPoint({ blobs: [slug, event], indexes: [slug] });
  return new Response(null, { status: 204 });
}

async function serveAsset(request: Request, env: Env, url: URL): Promise<Response> {
  // site_factory/build лежит без префикса assets: общий бандл и шрифты
  // публикуются как /assets/bundle.css и /assets/fonts/*, а в папке это
  // bundle.css и fonts/*.
  if (!env.ASSETS) return notFound();
  const inner = new URL(url);
  inner.pathname = url.pathname.slice("/assets".length) || "/";
  const response = await env.ASSETS.fetch(new Request(inner, request));
  return response.status === 404 ? notFound() : response;
}

async function servePreview(request: Request, env: Env, url: URL, slug: string): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", {
      status: 405,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  }
  if (!SLUG_RE.test(slug)) return notFound();

  let path: string;
  try {
    path = decodeURIComponent(url.pathname);
  } catch {
    return notFound();
  }
  if (path.endsWith("/")) path += "index.html";
  if (path.includes("..") || path.includes("//")) return notFound();

  const key = `${slug}${path}`;

  if (request.method === "HEAD") {
    const meta = await env.PREVIEWS.head(key);
    if (!meta) return notFound();
    const headers = new Headers();
    meta.writeHttpMetadata(headers);
    headers.set("etag", meta.httpEtag);
    headers.set("content-length", String(meta.size));
    return new Response(null, { headers });
  }

  const object = await env.PREVIEWS.get(key, { onlyIf: request.headers });
  if (!object) return notFound();
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  if (!("body" in object)) return new Response(null, { status: 304, headers });
  return new Response(object.body, { headers });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const slug = url.hostname.split(".")[0].toLowerCase();

    if (url.pathname === "/robots.txt") {
      // генерится здесь, а не лежит файлом: файл можно забыть положить в R2
      return secure(new Response("User-agent: *\nDisallow: /\n", {
        headers: { "content-type": "text/plain; charset=utf-8" },
      }));
    }
    if (url.pathname === "/api/lead") return secure(await handleLead(request, env, slug));
    if (url.pathname === "/api/hit") return secure(await handleHit(request, env, slug));
    if (url.pathname === "/assets" || url.pathname.startsWith("/assets/")) {
      return secure(await serveAsset(request, env, url));
    }
    return secure(await servePreview(request, env, url, slug));
  },
};
