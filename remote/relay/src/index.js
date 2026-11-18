// Forge Remote relay.
//
// GET /room/<id>?role=laptop|phone  (WebSocket)  join a room. Messages from one side are passed to
// the other side unchanged. They are encrypted end to end with a key only the paired laptop and
// phone know; the room id is derived from the same secret, so it can't be guessed.
// The relay tells each side when the other comes and goes ({"relay":"peer","laptop":true,...}).
import { DurableObject } from "cloudflare:workers";

const ROOM = /^[a-f0-9]{32,64}$/;
const MAX_FRAME = 4 * 1024 * 1024;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/room\/([^/]+)$/);
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response("forge relay ok\n", { headers: { "content-type": "text/plain" } });
    }
    if (!m || !ROOM.test(m[1])) return new Response("not found", { status: 404 });
    const role = url.searchParams.get("role");
    if (role !== "laptop" && role !== "phone") return new Response("bad role", { status: 400 });
    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("expected a websocket", { status: 426 });
    }
    return env.ROOMS.get(env.ROOMS.idFromName(m[1])).fetch(request);
  },
};

export class Room extends DurableObject {
  async fetch(request) {
    const role = new URL(request.url).searchParams.get("role");
    const [client, server] = Object.values(new WebSocketPair());
    if (role === "laptop") {
      // one laptop per room: a new connection replaces the old one
      for (const old of this.ctx.getWebSockets("laptop")) {
        try { old.close(4000, "replaced"); } catch {}
      }
    } else if (this.ctx.getWebSockets("phone").length >= 8) {
      return new Response("too many phones", { status: 429 });
    }
    this.ctx.acceptWebSocket(server, [role]);
    this.announce();
    return new Response(null, { status: 101, webSocket: client });
  }

  announce() {
    const live = (tag) => this.ctx.getWebSockets(tag).some((w) => w.readyState === 1);
    const msg = JSON.stringify({ relay: "peer", laptop: live("laptop"), phones: this.ctx.getWebSockets("phone").length });
    for (const ws of this.ctx.getWebSockets()) {
      try { ws.send(msg); } catch {}
    }
  }

  async webSocketMessage(ws, data) {
    const size = typeof data === "string" ? data.length : data.byteLength;
    if (size > MAX_FRAME) return;
    if (data === "ping") { try { ws.send("pong"); } catch {} return; }
    const role = this.ctx.getTags(ws)[0];
    const other = role === "laptop" ? "phone" : "laptop";
    for (const peer of this.ctx.getWebSockets(other)) {
      try { peer.send(data); } catch {}
    }
  }

  async webSocketClose(ws, code) {
    try { ws.close(code === 1005 ? 1000 : code, "bye"); } catch {}
    this.announce();
  }

  async webSocketError() {
    this.announce();
  }
}
