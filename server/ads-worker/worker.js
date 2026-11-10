// Forge sponsor-line server: Cloudflare Worker + D1.
// GET  /v1/ad?v=<forge version>&id=<random install id>  -> one signed ad, or {} (no ad)
// GET  /admin/stats                (Authorization: Bearer ADMIN_TOKEN) -> sponsors with shown counts
// POST /admin/sponsors  JSON body  (Authorization: Bearer ADMIN_TOKEN) -> add a sponsor
// POST /admin/sponsors/<id>/pause  (Authorization: Bearer ADMIN_TOKEN)
// Kill switch: set the ADS_ENABLED variable to "0" (dashboard or wrangler.toml) - every client gets {}.
// Forge only shows ads signed with AD_SIGNING_KEY; the matching public key is in forgecli/ads.py.

const AD_TTL_SECONDS = 3 * 24 * 3600;   // a cached ad stays valid this long on the user's computer
const PER_INSTALL_PER_DAY = 1;          // frequency cap

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

async function hashId(id) {
  // Store only a hash of the install id, never the id itself.
  return hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode("forge-ads:" + id))).slice(0, 32);
}

async function signingKey(env) {
  const der = Uint8Array.from(atob(env.AD_SIGNING_KEY), (c) => c.charCodeAt(0));
  return crypto.subtle.importKey("pkcs8", der, { name: "Ed25519" }, false, ["sign"]);
}

async function sign(env, ad) {
  // Must match forgecli/ads.py signed_message(): id, text, url, exp joined by newlines.
  const msg = new TextEncoder().encode([ad.id, ad.text, ad.url, ad.exp].join("\n"));
  const sig = await crypto.subtle.sign({ name: "Ed25519" }, await signingKey(env), msg);
  return hex(sig);
}

function authorised(request, env) {
  const h = request.headers.get("authorization") || "";
  return env.ADMIN_TOKEN && h === `Bearer ${env.ADMIN_TOKEN}`;
}

async function serveAd(url, env) {
  if (env.ADS_ENABLED === "0") return json({});
  const id = (url.searchParams.get("id") || "").slice(0, 64);
  if (!/^[0-9a-f]{8,64}$/.test(id)) return json({});
  const who = await hashId(id);
  const day = new Date().toISOString().slice(0, 10);

  const seen = await env.DB.prepare("SELECT count FROM caps WHERE who = ? AND day = ?").bind(who, day).first();
  if (seen && seen.count >= PER_INSTALL_PER_DAY) return json({});

  const now = Math.floor(Date.now() / 1000);
  const sp = await env.DB.prepare(
    `SELECT id, text, url FROM sponsors
     WHERE active = 1 AND starts <= ?1 AND ends >= ?1 AND shown < paid
     ORDER BY (shown * 1.0 / paid) ASC, RANDOM() LIMIT 1`
  ).bind(now).first();
  if (!sp) return json({});

  await env.DB.batch([
    env.DB.prepare("UPDATE sponsors SET shown = shown + 1 WHERE id = ?").bind(sp.id),
    env.DB.prepare(
      `INSERT INTO caps (who, day, count) VALUES (?1, ?2, 1)
       ON CONFLICT(who, day) DO UPDATE SET count = count + 1`
    ).bind(who, day),
  ]);
  const ad = { id: String(sp.id), text: sp.text, url: sp.url || "", exp: now + AD_TTL_SECONDS };
  ad.sig = await sign(env, ad);
  return json(ad);
}

async function admin(request, url, env) {
  if (!authorised(request, env)) return json({ error: "unauthorised" }, 401);
  if (request.method === "GET" && url.pathname === "/admin/stats") {
    const { results } = await env.DB.prepare(
      "SELECT id, name, text, url, starts, ends, paid, shown, active FROM sponsors ORDER BY id DESC"
    ).all();
    return json({ sponsors: results });
  }
  if (request.method === "POST" && url.pathname === "/admin/sponsors") {
    const b = await request.json();
    const text = String(b.text || "").trim();
    if (!text || text.length > 90) return json({ error: "text must be 1-90 characters" }, 400);
    if (b.url && !/^https:\/\/[A-Za-z0-9.\-]+(\/[A-Za-z0-9._~\/%?=&+\-#]*)?$/.test(b.url))
      return json({ error: "url must be a plain https link" }, 400);
    const r = await env.DB.prepare(
      "INSERT INTO sponsors (name, text, url, starts, ends, paid) VALUES (?, ?, ?, ?, ?, ?)"
    ).bind(String(b.name || ""), text, b.url || "", Number(b.starts) || 0, Number(b.ends) || 4102444800,
           Number(b.paid) || 0).run();
    return json({ ok: true, id: r.meta.last_row_id });
  }
  const m = url.pathname.match(/^\/admin\/sponsors\/(\d+)\/(pause|resume)$/);
  if (request.method === "POST" && m) {
    await env.DB.prepare("UPDATE sponsors SET active = ? WHERE id = ?").bind(m[2] === "resume" ? 1 : 0, Number(m[1])).run();
    return json({ ok: true });
  }
  return json({ error: "not found" }, 404);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === "GET" && url.pathname === "/v1/ad") return await serveAd(url, env);
      if (url.pathname.startsWith("/admin/")) return await admin(request, url, env);
      return json({ error: "not found" }, 404);
    } catch (e) {
      return url.pathname === "/v1/ad" ? json({}) : json({ error: String(e) }, 500);
    }
  },
};
