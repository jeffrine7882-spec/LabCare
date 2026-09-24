package com.insforge.labcare;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;
import android.app.NotificationManager;

import org.json.JSONObject;

public class MainActivity extends Activity {

    private final String API_BASE = "https://labcare.insforge.site";

    private SharedPreferences prefs;
    private LinearLayout root;
    private TextView statusText;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("labcare", MODE_PRIVATE);

        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(20), dp(24), dp(20), dp(24));
        root.setBackgroundColor(Color.rgb(243, 245, 249));

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(root);
        setContentView(scroll);

        render();
        if (isLoggedIn()) {
            ensureRelayRunning();
        }
        // Android 13+ needs the user to allow notifications before any alert
        // can appear (or ring).
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 100);
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

        TextView title = new TextView(this);
        title.setText("LabCare");
        title.setTextSize(32);
        title.setTypeface(null, Typeface.BOLD);
        title.setTextColor(Color.rgb(15, 118, 110));
        title.setGravity(Gravity.CENTER);
        root.addView(title);

        TextView sub = muted("Equipment complaints & breakdowns");
        sub.setGravity(Gravity.CENTER);
        root.addView(sub);
        LinearLayout.LayoutParams lp = (LinearLayout.LayoutParams) sub.getLayoutParams();
        lp.setMargins(0, dp(4), 0, dp(18));
        sub.setLayoutParams(lp);

        if (isLoggedIn()) {
            renderHome();
        } else {
            renderLogin();
        }
    }

    private void renderHome() {
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
        textCol.addView(muted("Rings for every bell alert, even with the screen off."));
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

        // Status card
        statusText = muted("Ready.");
        LinearLayout statusBox = new LinearLayout(this);
        statusBox.setOrientation(LinearLayout.VERTICAL);
        statusBox.addView(label("Status"));
        statusBox.addView(statusText);
        card(statusBox);

        Button signOut = button("Sign out", false, v -> {
            prefs.edit().remove("token").remove("email").remove("name").remove("uid").apply();
            stopRelay();
            render();
        });
        signOut.setBackgroundColor(Color.rgb(15, 118, 110));
        root.addView(signOut);

        TextView foot = muted("Alerts are fetched from your LabCare account every 10 seconds. " +
                "This early build rings while the app or its service is running. Danger-free " +
                "closed-app ringing via the LabCare server is added as soon as a Firebase config " +
                "is provided — see apps/android/README.md.");
        foot.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams flp = (LinearLayout.LayoutParams) foot.getLayoutParams();
        flp.setMargins(0, dp(16), 0, 0);
        foot.setLayoutParams(flp);
        root.addView(foot);
    }

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
                toast("Enter your LabCare email and password");
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
                        toast("Signed in \u2014 alerts on");
                        render();
                        ensureRelayRunning();
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

    // -------------------------------------------------------------- helpers

    boolean isLoggedIn() {
        String tok = prefs.getString("token", "");
        return !tok.isEmpty();
    }

    void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }

    private String[] doLogin(String email, String password) {
        try {
            JSONObject body = new JSONObject();
            body.put("email", email);
            body.put("password", password);
            String resp = httpPost("/api/login", body.toString(), null);
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

    private String httpPost(String path, String jsonBody, String token) throws Exception {
        java.net.URL url = new java.net.URL(API_BASE + path);
        java.net.HttpURLConnection c = (java.net.HttpURLConnection) url.openConnection();
        c.setRequestMethod("POST");
        c.setConnectTimeout(10000);
        c.setReadTimeout(20000);
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("Accept", "application/json");
        if (token != null) c.setRequestProperty("Authorization", "Bearer " + token);
        c.setDoOutput(true);
        c.getOutputStream().write(jsonBody.getBytes("UTF-8"));
        return readStream(c);
    }

    static String readStream(java.net.HttpURLConnection c) throws Exception {
        int code = c.getResponseCode();
        java.io.InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
        if (in == null) return "";
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        return new String(out.toByteArray(), "UTF-8");
    }

    // ---------------------------------------------------------------- relay

    void ensureRelayRunning() {
        if (!prefs.getBoolean("alerts", true)) return;
        if (!isLoggedIn()) return;
        Intent i = new Intent(this, AlertRelayService.class);
        i.setAction("START");
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(i);
        else startService(i);
    }

    void stopRelay() {
        Intent i = new Intent(this, AlertRelayService.class);
        i.setAction("STOP");
        startService(i);
        stopService(i);
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (isLoggedIn()) ensureRelayRunning();
    }
}
