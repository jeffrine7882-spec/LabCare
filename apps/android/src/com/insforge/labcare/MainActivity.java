package com.insforge.labcare;

import android.app.Activity;
import android.content.ClipData;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

public class MainActivity extends Activity {

    private SharedPreferences prefs;
    /** Native screens render into this column (inside {@link #scroll}). */
    private LinearLayout root;
    private ScrollView scroll;
    /** Screen area: shows either the site WebView or the native scroll view. */
    private FrameLayout content;
    /** The LabSynch site itself — kept alive across tab switches. */
    private WebView web;
    /** Which screen is shown: site | home | alerts | sound */
    private String screen = "site";
    private ValueCallback<Uri[]> fileCallback;
    /** True when the device has no usable WebView — fall back to the browser. */
    private boolean webDead = false;
    /** True when the last site load failed — show the fallback view. */
    private boolean webErrored = false;
    private String webErrorMsg = null;

    private static final int PICK_FILE = 300;

    /** The in-app alert list (fetched from /api/notifications). */
    static class Notif {
        int id;
        String text;
        String entityType;
        int entityId;
        boolean read;
        String created;
        Notif(int id, String text, String entityType, int entityId, boolean read, String created) {
            this.id = id; this.text = text; this.entityType = entityType;
            this.entityId = entityId; this.read = read; this.created = created;
        }
    }
    private final List<Notif> notifs = new ArrayList<>();
    private boolean notifsLoading = false;

    /**
     * The session can end outside this screen's own buttons: the relay drops a
     * token the API repeatedly confirmed dead, or the site bridge signs in /
     * out. Re-render the native screens so "Signed in as" never lies. (Held in
     * a field: the preference manager only keeps listeners weakly.)
     */
    private final SharedPreferences.OnSharedPreferenceChangeListener sessionWatcher =
            (p, key) -> {
                if (!"token".equals(key)) return;
                if (!"site".equals(screen)) render();
            };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("labcare", MODE_PRIVATE);
        prefs.registerOnSharedPreferenceChangeListener(sessionWatcher);
        if (getIntent() != null && "home".equals(getIntent().getStringExtra("screen"))) {
            screen = "home";            // e.g. tapped the "Signed out of LabSynch" notification
        }

        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(20), dp(24), dp(20), dp(8));
        root.setBackgroundColor(Color.rgb(243, 245, 249));

        scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(root);

        content = new FrameLayout(this);
        content.addView(scroll, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        LinearLayout shell = new LinearLayout(this);
        shell.setOrientation(LinearLayout.VERTICAL);
        shell.setBackgroundColor(Color.rgb(243, 245, 249));
        LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f);
        shell.addView(content, clp);
        shell.addView(buildNav());
        setContentView(shell);

        render();
        if (isLoggedIn()) {
            ensureRelayRunning();
            promptRingProtection();
        }
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 100);
        }
    }

    @Override
    protected void onDestroy() {
        try { prefs.unregisterOnSharedPreferenceChangeListener(sessionWatcher); } catch (Exception ignored) { }
        super.onDestroy();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent != null && "home".equals(intent.getStringExtra("screen"))) {
            screen = "home";
            render();
        }
    }

    // ------------------------------------------------------------------ UI

    private TextView label(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(14);
        t.setTextColor(Color.rgb(60, 60, 67));
        t.setTypeface(null, Typeface.BOLD);
        return t;
    }

    private TextView muted(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(13);
        t.setTextColor(Color.rgb(108, 117, 125));
        return t;
    }

    private EditText input(int inputType) {
        EditText e = new EditText(this);
        e.setInputType(inputType);
        e.setTextSize(15);
        e.setPadding(dp(12), dp(10), dp(12), dp(10));
        e.setBackgroundColor(Color.WHITE);
        e.setSingleLine(true);
        return e;
    }

    private Button button(String text, boolean primary, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setTextSize(15);
        b.setTypeface(null, Typeface.BOLD);
        b.setAllCaps(false);
        b.setTextColor(Color.WHITE);
        b.setBackgroundColor(primary ? Color.rgb(15, 118, 110) : Color.rgb(220, 38, 38));
        b.setOnClickListener(l);
        b.setPadding(dp(12), dp(12), dp(12), dp(12));
        return b;
    }

    private void card(View child) {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, 0, 0, dp(12));
        child.setLayoutParams(lp);
        child.setPadding(dp(18), dp(18), dp(18), dp(18));
        child.setBackgroundColor(Color.WHITE);
        root.addView(child);
    }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                getResources().getDisplayMetrics());
    }

    // --------------------------------------------------------------- render

    void render() {
        root.removeAllViews();
        content.removeAllViews();

        if ("site".equals(screen)) {
            content.addView(siteView(), new FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
            return;
        }

        TextView title = new TextView(this);
        title.setText("LabSynch");
        title.setTextSize(28);
        title.setTypeface(null, Typeface.BOLD);
        title.setTextColor(Color.rgb(15, 118, 110));
        title.setGravity(Gravity.CENTER);
        root.addView(title);

        TextView sub = muted(screenTitle());
        sub.setGravity(Gravity.CENTER);
        root.addView(sub);
        LinearLayout.LayoutParams lp = (LinearLayout.LayoutParams) sub.getLayoutParams();
        lp.setMargins(0, dp(4), 0, dp(14));
        sub.setLayoutParams(lp);

        if ("alerts".equals(screen)) renderAlerts();
        else if ("sound".equals(screen)) renderSound();
        else renderHome();

        content.addView(scroll, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
    }

    private String screenTitle() {
        switch (screen) {
            case "alerts": return "Notifications";
            case "sound": return "Alert sound";
            default: return isLoggedIn() ? "Phone alerts" : "Sign in";
        }
    }

    // ----------------------------------------------------------------- site

    /**
     * The LabSynch site itself, embedded so the app connects the user straight
     * to the web app (same account, same data). The native token is injected
     * as the site's own session (cookie + localStorage) so the site opens
     * already signed in.
     */
    private View siteView() {
        if (web == null) {
            try {
                web = createSiteWebView();
            } catch (Throwable t) {
                webDead = true;   // no WebView provider on this device
            }
        }
        if (webDead || web == null) {
            return siteFallbackView(
                    "This phone cannot display the site inside the app (no Android System WebView).");
        }
        if (webErrored) {
            return siteFallbackView(webErrorMsg);
        }
        syncSiteSession();
        return web;
    }

    /** Never strand the user: always offer a one-tap path to the real site. */
    private View siteFallbackView(String msg) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(28), dp(48), dp(28), dp(24));

        TextView head = label("The site didn't open in the app");
        box.addView(head);
        TextView m = muted(msg == null
                ? "The LabSynch site failed to load. Check your internet connection."
                : msg);
        LinearLayout.LayoutParams mlp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        mlp.setMargins(0, dp(8), 0, dp(8));
        m.setLayoutParams(mlp);
        box.addView(m);

        Button browser = button("Open the site in my browser", true,
                v -> openExternal(Uri.parse(Api.BASE + "/")));
        box.addView(browser);

        if (!webDead && web != null) {
            Button retry = button("Retry in the app", true, v -> {
                webErrored = false;
                webErrorMsg = null;
                web.loadUrl(Api.BASE + "/");
                render();
            });
            retry.setBackgroundColor(Color.rgb(220, 232, 230));
            retry.setTextColor(Color.rgb(15, 118, 110));
            LinearLayout.LayoutParams rlp = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
            rlp.setMargins(0, dp(10), 0, 0);
            retry.setLayoutParams(rlp);
            box.addView(retry);
        }
        return box;
    }

    private WebView createSiteWebView() {
        WebView w = new WebView(this);
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setSupportMultipleWindows(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        CookieManager.getInstance().setAcceptCookie(true);
        w.clearCache(true);

        w.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                webErrored = false;
                webErrorMsg = null;
                injectToken();
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                injectToken();
                captureTokenFromPage();
                view.evaluateJavascript(BLOB_DOWNLOAD_SHIM, null);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request == null || !request.isForMainFrame()) return;
                webErrored = true;
                CharSequence d = error != null ? error.getDescription() : null;
                webErrorMsg = (d == null || d.length() == 0)
                        ? "The LabSynch site failed to load. Check your internet connection."
                        : "The LabSynch site failed to load (" + d + ").";
                render();
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                Uri u = req.getUrl();
                String host = u.getHost() == null ? "" : u.getHost();
                if (host.equals("labcare.insforge.site") || host.endsWith(".insforge.site")) {
                    return false; // keep LabSynch pages in the app
                }
                openExternal(u);
                return true;
            }
        });

        w.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> cb,
                                             FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = cb;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                String type = "*/*";
                String[] accepts = params != null ? params.getAcceptTypes() : null;
                if (accepts != null && accepts.length > 0 && accepts[0] != null && !accepts[0].isEmpty()) {
                    type = accepts[0];
                }
                i.setType(type);
                if (params != null && params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) {
                    i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                }
                try {
                    startActivityForResult(Intent.createChooser(i, "Choose a file"), PICK_FILE);
                } catch (Exception e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }
        });

        w.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                        String mimetype, long contentLength) {
                if (url == null || url.startsWith("blob:")) return; // blobs handled in-page
                openExternal(Uri.parse(url));
            }
        });

        w.addJavascriptInterface(new SiteBridge(), "LabSynchDroid");

        // Load the site IMMEDIATELY and unconditionally — never gate the load
        // on a cookie callback. Session sync (cookie + localStorage) is
        // re-applied on every page event, so the user always reaches the site.
        CookieManager cm = CookieManager.getInstance();
        cm.setCookie(Api.BASE + "/", cookieValue());
        cm.flush();
        w.loadUrl(Api.BASE + "/");
        return w;
    }

    /** The site session cookie: 400 days (the WebView's cap); re-issued by the site on every visit. */
    private static final long SITE_COOKIE_MAX_AGE_S = 400L * 24 * 3600;

    /** Session cookie name matches the web app's HttpOnly auth cookie. */
    private String cookieValue() {
        String tok = prefs.getString("token", "");
        return tok.isEmpty()
                ? "labcare_token=; Path=/; Max-Age=0"
                : "labcare_token=" + tok + "; Path=/; Secure; Max-Age=" + SITE_COOKIE_MAX_AGE_S;
    }

    private void syncSiteSession() {
        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setCookie(Api.BASE + "/", cookieValue());
        cm.flush();
        injectToken();
    }

    /** Mirror the native token into the site's localStorage session. */
    private void injectToken() {
        if (web == null) return;
        String tok = prefs.getString("token", "");
        String js = "(function(){try{" +
                (tok.isEmpty()
                        ? "localStorage.removeItem('labcare_token');"
                        : "localStorage.setItem('labcare_token','" + tok + "');") +
                "}catch(e){}})();";
        web.evaluateJavascript(js, null);
    }

    /**
     * If the user signs in on the site itself (inside the WebView), pick that
     * session up so the alert relay can use it too.
     */
    private void captureTokenFromPage() {
        if (web == null) return;
        web.evaluateJavascript("(function(){try{return localStorage.getItem('labcare_token')||'';}catch(e){return '';}})();",
                value -> {
                    if (value == null || value.length() < 3 || "null".equals(value)) return;
                    String tok = value.substring(1, value.length() - 1);
                    if (tok.isEmpty() || tok.equals(prefs.getString("token", ""))) return;
                    prefs.edit().putString("token", tok).apply();
                    ensureRelayRunning();
                    toast("Phone alerts linked to your site session");
                });
    }

    private void openExternal(Uri u) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, u));
        } catch (Exception e) {
            toast("No app can open this link");
        }
    }

    /**
     * Saves blob downloads (e.g. PDF service reports) from the in-app site and
     * hands them to the system viewer via {@link FilesProvider}, and mirrors a
     * sign-in / sign-out the user makes INSIDE the site into this app, so the
     * phone alerts follow the site session without a second sign-in and a
     * manual sign-out on the site is a sign-out here too. (The web app calls
     * these only when the bridge exists; they are no-ops in a browser.)
     */
    private class SiteBridge {
        @JavascriptInterface
        public void signedIn(final String token, final String email, final String name) {
            if (token == null || token.length() < 3) return;
            runOnUiThread(() -> {
                if (token.equals(prefs.getString("token", ""))) return;
                prefs.edit()
                        .putString("token", token)
                        .putString("email", email == null ? "" : email)
                        .putString("name", name == null ? "" : name)
                        .apply();
                notifs.clear();
                syncSiteSession();          // cookie ← the new token, no reload needed
                ensureRelayRunning();
                toast("Phone alerts linked to your site session");
            });
        }

        @JavascriptInterface
        public void signedOut(final String token) {
            runOnUiThread(() -> {
                String cur = prefs.getString("token", "");
                if (cur.isEmpty()) return;
                // only the session this app shares with the site ends it here
                if (token != null && !token.isEmpty() && !token.equals(cur)) return;
                prefs.edit().remove("token").remove("email").remove("name").remove("uid").apply();
                notifs.clear();
                syncSiteSession();          // the site already cleared its own copy
                stopRelay();
            });
        }

        @JavascriptInterface
        public void save(final String name, final String b64, final String mime) {
            try {
                final byte[] data = android.util.Base64.decode(b64, android.util.Base64.DEFAULT);
                String safe = (name == null || name.isEmpty()) ? "download.bin" : new java.io.File(name).getName();
                final java.io.File f = new java.io.File(FilesProvider.baseDir(MainActivity.this), safe);
                java.io.FileOutputStream out = new java.io.FileOutputStream(f);
                out.write(data);
                out.close();
                final Intent i = new Intent(Intent.ACTION_VIEW);
                i.setDataAndType(FilesProvider.uriFor(MainActivity.this, f),
                        (mime == null || mime.isEmpty()) ? "*/*" : mime);
                i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
                runOnUiThread(() -> {
                    try {
                        startActivity(i);
                    } catch (Exception e) {
                        toast("Saved to app files: " + f.getName());
                    }
                });
            } catch (Exception e) {
                runOnUiThread(() -> toast("Could not save file"));
            }
        }
    }

    /** Capture blob <a download> clicks inside the site and route them out. */
    private static final String BLOB_DOWNLOAD_SHIM =
            "(function(){if(window.__lcDl)return;window.__lcDl=1;" +
            "document.addEventListener('click',function(e){" +
            "try{var t=e.target;var a=t&&t.closest?t.closest('a[download]'):null;" +
            "if(!a||!a.href||a.href.indexOf('blob:')!==0)return;" +
            "e.preventDefault();e.stopPropagation();" +
            "fetch(a.href).then(function(r){return r.blob()}).then(function(b){" +
            "var fr=new FileReader();fr.onload=function(){" +
            "LabSynchDroid.save(a.getAttribute('download')||'file.bin'," +
            "(fr.result||'').split(',')[1]||'',b.type||'application/octet-stream');" +
            "};fr.readAsDataURL(b);});" +
            "}catch(err){}" +
            "},true);})();";

    // ----------------------------------------------------------------- home

    private void renderHome() {
        if (!isLoggedIn()) {
            renderLogin();
            return;
        }
        LinearLayout who = new LinearLayout(this);
        who.setOrientation(LinearLayout.VERTICAL);
        who.addView(label("Signed in as"));
        who.addView(muted(prefs.getString("email", "")));
        card(who);

        // Relay / alert switch
        LinearLayout alertBox = new LinearLayout(this);
        alertBox.setOrientation(LinearLayout.VERTICAL);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout textCol = new LinearLayout(this);
        textCol.setOrientation(LinearLayout.VERTICAL);
        textCol.addView(label("Phone alerts"));
        textCol.addView(muted("Rings for every bell alert — even with the app closed, screen off or after a reboot."));
        LinearLayout.LayoutParams tlp = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        textCol.setLayoutParams(tlp);

        Switch sw = new Switch(this);
        sw.setChecked(prefs.getBoolean("alerts", true));
        sw.setOnCheckedChangeListener((buttonView, isChecked) -> {
            prefs.edit().putBoolean("alerts", isChecked).apply();
            if (isChecked) ensureRelayRunning();
            else stopRelay();
            toast(isChecked ? "Alerts on" : "Alerts off");
        });
        row.addView(textCol);
        row.addView(sw);
        alertBox.addView(row);
        card(alertBox);

        // Battery / alarm / notification settings that decide whether the
        // relay truly rings with the app closed — with one-tap fixes.
        renderRingProtection();

        // Quick actions
        LinearLayout quick = new LinearLayout(this);
        quick.setOrientation(LinearLayout.HORIZONTAL);
        quick.setGravity(Gravity.CENTER);
        quick.addView(miniNav("Open the site", () -> { screen = "site"; render(); }));
        quick.addView(miniNav("Ring sound", () -> { screen = "sound"; render(); }));
        card(quick);

        Button signOut = button("Sign out", false, v -> {
            final String tok = prefs.getString("token", "");
            new Thread(() -> {
                try { Api.post("/api/logout", "{}", tok); } catch (Exception ignored) { }
            }).start();
            prefs.edit().remove("token").remove("email").remove("name").remove("uid").apply();
            syncSiteSession();               // clears the site session too
            if (web != null) web.loadUrl(Api.BASE + "/");
            screen = "site";
            notifs.clear();
            stopRelay();
            render();
        });
        signOut.setBackgroundColor(Color.rgb(15, 118, 110));
        root.addView(signOut);
    }

    /**
     * One-time ask for the battery-optimisation exemption — the single
     * setting that decides whether Android's Doze/App Standby let the relay
     * keep ringing with the app closed. The Home "Ring protection" card
     * keeps offering the fix afterwards.
     */
    private void promptRingProtection() {
        try {
            android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null && pm.isIgnoringBatteryOptimizations(getPackageName())) return;
            if (prefs.getBoolean("battery_asked", false)) return;
            prefs.edit().putBoolean("battery_asked", true).apply();
            new android.app.AlertDialog.Builder(this)
                    .setTitle("Keep alerts ringing")
                    .setMessage("To ring even when the app is closed, Android must let LabSynch run without battery restrictions.\n\nTap Allow on the next screen.")
                    .setPositiveButton("Allow", (d, w) -> requestBatteryExemption())
                    .setNegativeButton("Later", (d, w) -> d.dismiss())
                    .setCancelable(true)
                    .show();
        } catch (Exception ignored) {
        }
    }

    private void requestBatteryExemption() {
        try {
            startActivity(new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            try {
                startActivity(new Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Exception ignored) {
            }
        }
    }

    /** Status + one-tap fixes for everything closed-app ringing depends on. */
    private void renderRingProtection() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.addView(label("Ring protection"));
        boolean allGood = true;

        // Notifications (banner + vibration; the sound rings regardless)
        try {
            android.app.NotificationManager nm = (android.app.NotificationManager)
                    getSystemService(NOTIFICATION_SERVICE);
            if (nm != null && !nm.areNotificationsEnabled()) {
                allGood = false;
                box.addView(muted("Notifications blocked — the alert banner won't show (the sound still rings)."));
                box.addView(fixButton("Allow notifications", v -> {
                    try {
                        startActivity(new Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                                .putExtra("android.app.extra.APP_PACKAGE", getPackageName()));
                    } catch (Exception ignored) {
                    }
                }));
            }
        } catch (Exception ignored) {
        }

        // Battery optimisation — the big one for Doze
        try {
            android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
            if (pm == null || !pm.isIgnoringBatteryOptimizations(getPackageName())) {
                allGood = false;
                box.addView(muted("Battery restricted — Android may delay alerts while the phone idles."));
                box.addView(fixButton("Remove battery restriction", v -> requestBatteryExemption()));
            }
        } catch (Exception ignored) {
        }

        // Exact alarms (Android 12+) — Doze-proof wake-ups + resurrect rights
        if (Build.VERSION.SDK_INT >= 31 && !Alarms.canExact(this)) {
            allGood = false;
            box.addView(muted("Alarms & reminders off — wake-up checks may be delayed."));
            box.addView(fixButton("Allow alarms & reminders", v -> {
                try {
                    startActivity(new Intent(android.provider.Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,
                            Uri.parse("package:" + getPackageName())));
                } catch (Exception ignored) {
                }
            }));
        }

        // Full-screen alerts (Android 14+)
        if (Build.VERSION.SDK_INT >= 34) {
            try {
                android.app.NotificationManager nm = (android.app.NotificationManager)
                        getSystemService(NOTIFICATION_SERVICE);
                if (nm != null && !nm.canUseFullScreenIntent()) {
                    allGood = false;
                    box.addView(muted("Full-screen alerts off — locked-screen alerts show as a normal banner."));
                    box.addView(fixButton("Allow full-screen alerts", v -> {
                        try {
                            startActivity(new Intent(android.provider.Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
                                    Uri.parse("package:" + getPackageName())));
                        } catch (Exception ignored) {
                        }
                    }));
                }
            } catch (Exception ignored) {
            }
        }

        if (allGood) {
            box.addView(muted("All set — alerts ring even when the app is closed, the screen is off, the app is swiped away or the phone reboots."));
        }
        card(box);
    }

    private Button fixButton(String text, View.OnClickListener l) {
        Button b = button(text, true, l);
        b.setPadding(dp(12), dp(8), dp(12), dp(8));
        b.setTextSize(13);
        return b;
    }

    // ------------------------------------------------------------- alerts

    private void renderAlerts() {
        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setGravity(Gravity.END);
        Button markAll = button("Mark all read", true, v -> markAllRead());
        markAll.setPadding(dp(12), dp(8), dp(12), dp(8));
        markAll.setTextSize(13);
        actions.addView(markAll);
        card(actions);

        LinearLayout listBox = new LinearLayout(this);
        listBox.setOrientation(LinearLayout.VERTICAL);
        if (!isLoggedIn()) {
            listBox.addView(muted("Sign in on the Home tab (or use the site) to see notifications."));
        } else if (notifsLoading) {
            listBox.addView(muted("Loading…"));
        } else if (notifs.isEmpty()) {
            listBox.addView(muted("You're all caught up 🎉"));
        } else {
            for (Notif n : notifs) {
                LinearLayout item = new LinearLayout(this);
                item.setOrientation(LinearLayout.VERTICAL);
                item.setPadding(0, dp(10), 0, dp(10));

                TextView txt = new TextView(this);
                txt.setText(n.text);
                txt.setTextSize(14);
                txt.setTextColor(n.read ? Color.rgb(108, 117, 125) : Color.rgb(17, 24, 39));
                txt.setTypeface(null, n.read ? Typeface.NORMAL : Typeface.BOLD);
                item.addView(txt);

                TextView meta = new TextView(this);
                meta.setText((n.read ? "" : "● ") + n.created);
                meta.setTextSize(12);
                meta.setTextColor(Color.rgb(148, 163, 184));
                item.addView(meta);

                item.setOnClickListener(v -> {
                    if (!n.read) markRead(n);
                });

                View divider = new View(this);
                divider.setBackgroundColor(Color.rgb(229, 231, 235));
                LinearLayout.LayoutParams dlp = new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT, 1);
                divider.setLayoutParams(dlp);
                listBox.addView(item);
                listBox.addView(divider);
            }
        }
        card(listBox);
        if (isLoggedIn() && !notifsLoading) fetchNotifs();
    }

    // -------------------------------------------------------------- sound

    private void renderSound() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);

        box.addView(label("Ring sound"));
        box.addView(muted("The sound this phone plays when a new alert arrives."));

        RadioGroup group = new RadioGroup(this);
        group.setOrientation(RadioGroup.VERTICAL);

        String current = currentSound();
        String[] ids = {"chime", "bell", "beep", "alarm"};
        String[] names = {"Chime (default)", "Bell", "Triple beep", "Siren"};
        final int[] resIds = {R.raw.chime, R.raw.bell, R.raw.beep, R.raw.alarm};

        for (int i = 0; i < ids.length; i++) {
            final String id = ids[i];
            final int resId = resIds[i];
            RadioButton rb = new RadioButton(this);
            rb.setText(names[i]);
            rb.setTextSize(15);
            rb.setChecked(id.equals(current));
            rb.setPadding(0, dp(6), 0, dp(6));
            rb.setOnClickListener(v -> {
                prefs.edit().putString("sound", id).apply();
                prefs.edit().putInt("sound_res", resId).apply();
                playSound(resId);
            });
            group.addView(rb);
        }
        box.addView(group);
        card(box);

        Button test = button("▶ Test sound", true, v -> playSound(soundResId()));
        root.addView(test);
    }

    // -------------------------------------------------------------- nav

    private Button miniNav(String text, Runnable onClick) {
        Button b = new Button(this);
        b.setText(text);
        b.setTextSize(13);
        b.setTypeface(null, Typeface.BOLD);
        b.setAllCaps(false);
        b.setTextColor(Color.rgb(15, 118, 110));
        b.setBackgroundColor(Color.rgb(199, 242, 236));
        b.setOnClickListener(v -> onClick.run());
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        lp.setMargins(dp(4), 0, dp(4), 0);
        b.setLayoutParams(lp);
        return b;
    }

    private View buildNav() {
        LinearLayout nav = new LinearLayout(this);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setGravity(Gravity.CENTER);
        nav.addView(navBtn("Site", "site"));
        nav.addView(navBtn("Home", "home"));
        nav.addView(navBtn("Alerts", "alerts"));
        nav.addView(navBtn("Sound", "sound"));
        nav.setBackgroundColor(Color.rgb(229, 231, 235));
        nav.setPadding(0, dp(6), 0, dp(6));
        return nav;
    }

    private Button navBtn(String text, String target) {
        Button b = new Button(this);
        b.setText(text);
        b.setTextSize(14);
        b.setTypeface(null, Typeface.BOLD);
        b.setAllCaps(false);
        b.setTextColor(screen.equals(target) ? Color.WHITE : Color.rgb(15, 118, 110));
        b.setBackgroundColor(screen.equals(target) ? Color.rgb(15, 118, 110) : Color.TRANSPARENT);
        b.setOnClickListener(v -> {
            if ("site".equals(target) && "site".equals(screen) && web != null) {
                web.clearCache(true);
                web.reload();
                toast("Refreshing LabSynch…");
                return;
            }
            screen = target;
            render();
        });
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        b.setLayoutParams(lp);
        return b;
    }

    // ------------------------------------------------------------- login

    private void renderLogin() {
        LinearLayout form = new LinearLayout(this);
        form.setOrientation(LinearLayout.VERTICAL);

        form.addView(label("Email"));
        EditText email = input(InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS);
        email.setText(prefs.getString("saved_email", ""));
        form.addView(email);

        form.addView(label("Password"));
        EditText pass = input(InputType.TYPE_TEXT_VARIATION_PASSWORD | InputType.TYPE_CLASS_TEXT);
        form.addView(pass);
        pass.setSingleLine(true);

        Button signIn = button("Sign in", true, null);

        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        blp.setMargins(0, dp(14), 0, 0);

        signIn.setOnClickListener(v -> {
            String em = email.getText().toString().trim();
            String pw = pass.getText().toString();
            if (em.isEmpty() || pw.isEmpty()) {
                toast("Enter your LabSynch email and password");
                return;
            }
            signIn.setEnabled(false);
            signIn.setText("Signing in\u2026");
            prefs.edit().putString("saved_email", em).apply();
            new Thread(() -> {
                final String[] res = doLogin(em, pw);
                runOnUiThread(() -> {
                    signIn.setEnabled(true);
                    signIn.setText("Sign in");
                    if ("ok".equals(res[0])) {
                        toast("Signed in \u2014 opening LabSynch");
                        notifs.clear();
                        syncSiteSession();
                        if (web != null) web.loadUrl(Api.BASE + "/");
                        screen = "site";
                        render();
                        ensureRelayRunning();
                        promptRingProtection();
                    } else if ("pending".equals(res[0])) {
                        toast("Your account is awaiting administrator approval");
                    } else {
                        toast("Sign in failed: " + res[1]);
                    }
                });
            }).start();
        });

        signIn.setLayoutParams(blp);
        form.addView(signIn);
        card(form);

        TextView note = muted("Sign in with the same account you use on the web app.");
        note.setGravity(Gravity.CENTER);
        root.addView(note);
    }

    // ------------------------------------------------------------ helpers

    boolean isLoggedIn() {
        return !prefs.getString("token", "").isEmpty();
    }

    void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }

    String currentSound() {
        return prefs.getString("sound", "chime");
    }

    int soundResId() {
        int r = prefs.getInt("sound_res", 0);
        if (r != 0) return r;
        switch (currentSound()) {
            case "bell": return R.raw.bell;
            case "beep": return R.raw.beep;
            case "alarm": return R.raw.alarm;
            default: return R.raw.chime;
        }
    }

    void playSound(int resId) {
        try {
            android.media.MediaPlayer mp = android.media.MediaPlayer.create(
                    this, resId, new android.media.AudioAttributes.Builder()
                            .setUsage(android.media.AudioAttributes.USAGE_NOTIFICATION).build(), 1);
            if (mp != null) {
                mp.setOnCompletionListener(android.media.MediaPlayer::release);
                mp.start();
            }
        } catch (Exception e) { /* ignore */ }
    }

    private String[] doLogin(String email, String password) {
        try {
            JSONObject body = new JSONObject();
            body.put("email", email);
            body.put("password", password);
            String resp = Api.post("/api/login", body.toString(), null);
            JSONObject j = new JSONObject(resp);
            if (j.has("error")) {
                String err = j.getString("error");
                if (err.toLowerCase().contains("approval") || err.toLowerCase().contains("approve")) {
                    return new String[]{"pending", err};
                }
                return new String[]{"err", err};
            }
            JSONObject u = j.optJSONObject("user");
            prefs.edit()
                    .putString("token", j.getString("token"))
                    .putString("email", email)
                    .putString("name", u != null ? u.optString("name", "") : "")
                    .putString("uid", String.valueOf(u != null ? u.optInt("id", 0) : 0))
                    .apply();
            return new String[]{"ok", ""};
        } catch (Exception e) {
            return new String[]{"err", e.getMessage() == null ? "network error" : e.getMessage()};
        }
    }

    // ------------------------------------------------- notifications (API)

    private void fetchNotifs() {
        if (notifsLoading) return;
        notifsLoading = true;
        new Thread(() -> {
            final String token = prefs.getString("token", "");
            final List<Notif> out = new ArrayList<>();
            String err = null;
            try {
                JSONArray arr = new JSONArray(Api.get("/api/notifications", token));
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.getJSONObject(i);
                    out.add(new Notif(
                            o.optInt("id", 0),
                            o.optString("text", ""),
                            o.optString("entity_type", ""),
                            o.optInt("entity_id", 0),
                            o.optInt("read", 0) == 1,
                            o.optString("created_at", "")));
                }
            } catch (Exception e) {
                err = e.getMessage();
            }
            final String ferr = err;
            runOnUiThread(() -> {
                notifsLoading = false;
                if (ferr == null) {
                    notifs.clear();
                    notifs.addAll(out);
                }
                if ("alerts".equals(screen)) renderAlerts();
            });
        }).start();
    }

    private void markRead(final Notif n) {
        n.read = true;
        if ("alerts".equals(screen)) renderAlerts();
        new Thread(() -> {
            try {
                JSONObject b = new JSONObject();
                b.put("id", n.id);
                Api.post("/api/notifications/read", b.toString(), prefs.getString("token", ""));
            } catch (Exception ignored) { }
        }).start();
    }

    private void markAllRead() {
        for (Notif n : notifs) n.read = true;
        renderAlerts();
        new Thread(() -> {
            try {
                Api.post("/api/notifications/read", "{}", prefs.getString("token", ""));
            } catch (Exception ignored) { }
        }).start();
    }

    // --------------------------------------------------------------- relay

    void ensureRelayRunning() {
        if (!prefs.getBoolean("alerts", true)) return;
        if (!isLoggedIn()) return;
        // an explicit start clears any earlier deliberate stop, so the
        // watchdog chains are allowed to resurrect the relay again
        prefs.edit().putBoolean("relay_user_stopped", false).apply();
        Intent i = new Intent(this, AlertRelayService.class);
        i.setAction("START");
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(i);
        else startService(i);
    }

    void stopRelay() {
        // Remember the deliberate stop BEFORE tearing anything down, or the
        // watchdog would immediately resurrect the relay.
        prefs.edit().putBoolean("relay_user_stopped", true).apply();
        Alarms.cancelAll(this);
        Intent i = new Intent(this, AlertRelayService.class);
        i.setAction("STOP");
        try {
            startService(i);
            stopService(i);
        } catch (Exception ignored) {
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (isLoggedIn()) ensureRelayRunning();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != PICK_FILE || fileCallback == null) return;
        Uri[] result = null;
        if (resultCode == Activity.RESULT_OK && data != null) {
            Uri u = data.getData();
            if (u != null) {
                result = new Uri[]{u};
            } else {
                ClipData cd = data.getClipData();
                if (cd != null && cd.getItemCount() > 0) {
                    result = new Uri[cd.getItemCount()];
                    for (int i = 0; i < cd.getItemCount(); i++) {
                        result[i] = cd.getItemAt(i).getUri();
                    }
                }
            }
        }
        fileCallback.onReceiveValue(result);
        fileCallback = null;
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && isLoggedIn() && !"site".equals(screen)) {
            screen = "site";   // back returns to the site first
            render();
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && "site".equals(screen)
                && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
