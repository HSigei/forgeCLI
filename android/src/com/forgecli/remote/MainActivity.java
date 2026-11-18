package com.forgecli.remote;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.IOException;
import java.io.InputStream;

/**
 * Forge Remote: the phone app from docs/app, bundled. It's served from a secure origin
 * (https://appassets.androidplatform.net/app/) so the camera (QR pairing) and WebCrypto work, and the
 * pairing secret stays in this app's own storage.
 */
public class MainActivity extends Activity {
    static final String HOST = "appassets.androidplatform.net";
    static final String HOME = "https://" + HOST + "/app/index.html";
    static final int CAMERA_REQUEST = 7;

    WebView web;
    PermissionRequest waiting;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.rgb(10, 9, 18));
        getWindow().setNavigationBarColor(Color.rgb(10, 9, 18));
        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(10, 9, 18));
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if (!HOST.equals(u.getHost()) || u.getPath() == null || !u.getPath().startsWith("/app/")) return null;
                String path = u.getPath().substring(1);
                if (path.equals("app/")) path = "app/index.html";
                try {
                    InputStream in = getAssets().open(path);
                    return new WebResourceResponse(mime(path), "utf-8", in);
                } catch (IOException e) {
                    return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", null, null);
                }
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                if (HOST.equals(u.getHost())) return false;
                if (pairLink(u)) { pair(u.toString()); return true; }
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
                return true;
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(new Runnable() { public void run() {
                    boolean wantsCamera = false;
                    for (String r : request.getResources())
                        if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) wantsCamera = true;
                    if (!wantsCamera || !HOST.equals(request.getOrigin().getHost())) { request.deny(); return; }
                    if (Build.VERSION.SDK_INT < 23
                            || checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                        request.grant(new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});
                    } else {
                        waiting = request;
                        requestPermissions(new String[]{Manifest.permission.CAMERA}, CAMERA_REQUEST);
                    }
                }});
            }
        });

        Uri link = getIntent() != null ? getIntent().getData() : null;
        if (state != null) web.restoreState(state);
        else if (link != null && pairLink(link)) web.loadUrl(HOME + "#" + link.getFragment());
        else web.loadUrl(HOME);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        Uri link = intent.getData();
        if (link != null && pairLink(link)) pair(link.toString());
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code != CAMERA_REQUEST || waiting == null) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED)
            waiting.grant(new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});
        else
            waiting.deny();
        waiting = null;
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    public void onBackPressed() {
        moveTaskToBack(true);   // keep the connection; the app is a single screen
    }

    static boolean pairLink(Uri u) {
        String f = u.getFragment();
        return f != null && f.startsWith("pair=");
    }

    void pair(String link) {
        String js = "window.forgePair && window.forgePair(" + quote(link) + ")";
        web.evaluateJavascript(js, null);
    }

    static String quote(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c < 0x20 || c == 0x2028 || c == 0x2029) b.append(String.format("\\u%04x", (int) c));
            else b.append(c);
        }
        return b.append('"').toString();
    }

    static String mime(String path) {
        if (path.endsWith(".html")) return "text/html";
        if (path.endsWith(".js")) return "text/javascript";
        if (path.endsWith(".css")) return "text/css";
        if (path.endsWith(".svg")) return "image/svg+xml";
        if (path.endsWith(".png")) return "image/png";
        if (path.endsWith(".webmanifest") || path.endsWith(".json")) return "application/json";
        return "application/octet-stream";
    }
}
