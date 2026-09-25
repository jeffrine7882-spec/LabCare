package com.insforge.labcare;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
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
    private LinearLayout root;
    /** Which screen is shown: home | alerts | sound */
    private String screen = "home";

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

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("labcare", MODE_PRIVATE);

        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(20), dp(24), dp(20), dp(8));
        root.setBackgroundColor(Color.rgb(243, 245, 249));

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(root);
        setContentView(scroll);

        render();
        if (isLoggedIn()) {
            ensureRelayRunning();
        }
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
        title.setTextSize(28);
        title.setTypeface(null, Typeface.BOLD);
        title.setTextColor(Color.rgb(15, 118, 110));
        title.setGravity(Gravity.CENTER);
        root.addView(title);

        if (isLoggedIn()) {
            TextView sub = muted(screenTitle());
            sub.setGravity(Gravity.CENTER);
            root.addView(sub);
            LinearLayout.LayoutParams lp = (LinearLayout.LayoutParams) sub.getLayoutParams();
            lp.setMargins(0, dp(4), 0, dp(14));
            sub.setLayoutParams(lp);

            if ("alerts".equals(screen)) renderAlerts();
            else if ("sound".equals(screen)) renderSound();
            else renderHome();
            renderNav();
        } else {
            TextView sub = muted("Equipment complaints & breakdowns");
            sub.setGravity(Gravity.CENTER);
            root.addView(sub);
            LinearLayout.LayoutParams lp = (LinearLayout.LayoutParams) sub.getLayoutParams();
            lp.setMargins(0, dp(4), 0, dp(18));
            sub.setLayoutParams(lp);
            renderLogin();
        }
    }

    private String screenTitle() {
        switch (screen) {
            case "alerts": return "Notifications";
            case "sound": return "Alert sound";
            default: return "Phone alerts";
        }
    }

    // ----------------------------------------------------------------- home

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

        // Quick actions
        LinearLayout quick = new LinearLayout(this);
        quick.setOrientation(LinearLayout.HORIZONTAL);
        quick.setGravity(Gravity.CENTER);
        quick.addView(miniNav("Notifications", () -> { screen = "alerts"; render(); }));
        quick.addView(miniNav("Ring sound", () -> { screen = "sound"; render(); }));
        card(quick);

        Button signOut = button("Sign out", false, v -> {
            prefs.edit().remove("token").remove("email").remove("name").remove("uid").apply();
            screen = "home";
            notifs.clear();
            stopRelay();
            render();
        });
        signOut.setBackgroundColor(Color.rgb(15, 118, 110));
        root.addView(signOut);
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
        if (notifsLoading) {
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
                listBox.addView(item);
                listBox.addView(divider);
            }
        }
        card(listBox);
        if (!notifsLoading) fetchNotifs();
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

    private void renderNav() {
        LinearLayout nav = new LinearLayout(this);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setGravity(Gravity.CENTER);
        nav.addView(navBtn("Home", "home"));
        nav.addView(navBtn("Alerts", "alerts"));
        nav.addView(navBtn("Sound", "sound"));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.setMargins(0, dp(6), 0, dp(10));
        nav.setLayoutParams(lp);
        nav.setBackgroundColor(Color.rgb(229, 231, 235));
        nav.setPadding(0, dp(6), 0, dp(6));
        root.addView(nav);
    }

    private Button navBtn(String text, String target) {
        Button b = new Button(this);
        b.setText(text);
        b.setTextSize(14);
        b.setTypeface(null, Typeface.BOLD);
        b.setAllCaps(false);
        b.setTextColor(screen.equals(target) ? Color.WHITE : Color.rgb(15, 118, 110));
        b.setBackgroundColor(screen.equals(target) ? Color.rgb(15, 118, 110) : Color.TRANSPARENT);
        b.setOnClickListener(v -> { screen = target; render(); });
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
                        notifs.clear();
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
