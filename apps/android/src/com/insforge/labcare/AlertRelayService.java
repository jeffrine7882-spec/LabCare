package com.insforge.labcare;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;

import org.json.JSONObject;

/**
 * Foreground relay: polls the LabSynch bell endpoint and, on a new unread
 * notification, drops a HEADS-UP bubble over the top of the display, rings
 * the user's chosen sound and vibrates — the phone alerts even when the
 * LabSynch app UI is closed, swiped away or the screen is off.
 *
 * "Rings even when the app is closed" is enforced from five sides:
 *  1. the foreground service itself (Android does not idle-kill it),
 *  2. a partial wake lock held across every poll and ring — a foreground
 *     service alone does not keep the CPU out of suspend,
 *  3. a WAKE alarm every ~12 s ({@link Alarms}) that fires even in Doze,
 *     because Handler timers freeze once the CPU sleeps,
 *  4. restart alarms from onTaskRemoved()/onDestroy() — swiping the app
 *     away or an OEM killer only silences the relay for a couple of seconds,
 *  5. a WATCHDOG chain that resurrects the relay if its heartbeat ever
 *     goes stale.
 * All chains are cancelled when the user turns alerts off or signs out.
 *
 * The sign-in is kept until the user signs out. The relay never drops the
 * session on its own: a server restart or deploy, an outage, a proxy or
 * captive-portal 401, or a network blip only means "no alerts until it is
 * back". The single exception is a session the LabSynch API itself reports
 * as gone (the user signed out of LabSynch inside the site, or an
 * administrator removed the account): that needs
 * {@link #AUTH_CONFIRMATIONS_TO_SIGN_OUT} CONSECUTIVE polls in a row, each
 * re-confirmed by /api/me, before the dead token is dropped — and then the
 * user is told with a notification instead of the phone silently never
 * ringing again.
 */
public class AlertRelayService extends Service {

    private static final String CHANNEL = "labcare_alerts_v2"; // new id: heads-up settings land on upgrades too
    private static final int SIGNED_OUT_NOTIF_ID = 1002;
    /** ≈ 1 minute of the API consistently saying the session does not exist. */
    static final int AUTH_CONFIRMATIONS_TO_SIGN_OUT = 6;
    private Handler handler;
    private Runnable poller;
    private SharedPreferences prefs;
    private long lastId = -1;
    private boolean polling = false;
    /** consecutive polls in which the API confirmed the session no longer exists */
    private int authFailures = 0;

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences("labcare", MODE_PRIVATE);
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();

        if ("STOP".equals(action)) {
            // Deliberate stop ("Phone alerts" off / sign out): everything must
            // stay down — no sticky restart, no watchdog resurrection.
            prefs.edit().putBoolean("relay_user_stopped", true).apply();
            Alarms.cancelAll(this);
            if (handler != null && poller != null) handler.removeCallbacks(poller);
            handler = null;
            poller = null;
            lastId = -1;
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }

        // startForegroundService() contract: startForeground() promptly, even
        // on the bail-out paths below.
        startForeground(1001, buildOngoingNotification());

        if (!Alarms.wanted(this)) {
            // signed out / alerts off / user-stopped — nothing to relay
            Alarms.cancelAll(this);
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }

        if (handler == null) {
            startPolling();               // fresh start (START, sticky restart, resurrect)
        } else if ("WAKE".equals(action)) {
            pollOnce();                   // Doze alarm nudge while already running
        } else {
            pollOnce();                   // explicit START refresh
        }
        return START_STICKY;
    }

    private Notification buildOngoingNotification() {
        return new Notification.Builder(this, CHANNEL)
                .setContentTitle("LabSynch alerts on")
                .setContentText("Listening for equipment complaints & breakdowns")
                .setSmallIcon(R.drawable.ic_stat_bell)
                .setOngoing(true)
                .build();
    }

    private void startPolling() {
        handler = new Handler(Looper.getMainLooper());
        poller = new Runnable() {
            @Override
            public void run() {
                pollOnce();
                if (handler != null) handler.postDelayed(this, 10000);
            }
        };
        handler.post(poller);
        // Doze-proof chains (re-armed by WatchdogReceiver on every fire):
        Alarms.schedule(this, Alarms.ACTION_WAKE, 12000);
        Alarms.schedule(this, Alarms.ACTION_WATCHDOG, 60000);
    }

    private void pollOnce() {
        if (polling) return; // a WAKE alarm and the loop can overlap — never pile up
        polling = true;
        // Hold the CPU awake for the whole check; without this the CPU can
        // suspend mid-request the moment the screen is off.
        PowerManager.WakeLock lock = acquire("labcare:poll", 15000);
        new Thread(() -> {
            try {
                doPoll();
            } finally {
                polling = false;
                release(lock);
            }
        }).start();
    }

    private void doPoll() {
        String token = prefs.getString("token", "");
        if (token.isEmpty()) return;
        try {
            Api.Response r = Api.fetch("/api/notifications/ping", token);
            if (r.isApiAuthFailure()) {
                // maybe the session is gone — never decided on one answer
                onSessionRejected(token);
                return;
            }
            if (r.code >= 400) return;      // 5xx / proxy 401 / 403 / 429: transient, stay signed in
            JSONObject j = new JSONObject(r.body);
            if (j.has("error")) return;
            authFailures = 0;
            JSONObject latest = j.optJSONObject("latest");
            int unread = j.optInt("unread", 0);
            if (latest == null) return;
            long id = latest.optLong("id", -1);
            if (lastId == -1) {
                lastId = id;         // baseline — no burst on first sync
                return;
            }
            if (id != lastId && unread > 0 && prefs.getBoolean("alerts", true)) {
                lastId = id;
                String text = latest.optString("text", "New LabSynch alert");
                ring(text);
            } else if (id != lastId) {
                lastId = id;
            }
        } catch (Exception ignored) {
        } finally {
            // Heartbeat even on failure, so the watchdog can tell "alive but
            // offline" apart from "dead".
            try {
                prefs.edit().putLong("relay_heartbeat",
                        SystemClock.elapsedRealtime()).apply();
            } catch (Exception ignored) {
            }
        }
    }

    /**
     * The bell endpoint answered with the API's own 401. Re-check against
     * /api/me and count only consecutive confirmations; anything else (a
     * reachable /api/me that accepts the token, a non-JSON answer, a network
     * error) keeps the user signed in and the counter untouched or reset.
     */
    private void onSessionRejected(String token) {
        Api.Response me;
        try {
            me = Api.fetch("/api/me", token);
        } catch (Exception e) {
            return;                         // unreachable: no verdict either way
        }
        if (me.code >= 200 && me.code < 300) {
            authFailures = 0;               // the session is fine after all
            return;
        }
        if (!me.isApiAuthFailure()) return; // proxy / outage / odd answer: no verdict
        authFailures++;
        if (authFailures < AUTH_CONFIRMATIONS_TO_SIGN_OUT) return;
        if (!token.equals(prefs.getString("token", ""))) return; // re-signed-in meanwhile
        new Handler(Looper.getMainLooper()).post(this::endSession);
    }

    /**
     * The token is dead for good (signed out on the site / account removed):
     * drop it, stop every chain and TELL the user — a relay that keeps
     * polling with a dead token would simply never ring again.
     */
    private void endSession() {
        authFailures = 0;
        prefs.edit()
                .remove("token").remove("email").remove("name").remove("uid")
                .putBoolean("relay_user_stopped", true)   // watchdog must not resurrect us
                .apply();
        Alarms.cancelAll(this);
        if (handler != null && poller != null) handler.removeCallbacks(poller);
        handler = null;
        poller = null;
        lastId = -1;
        notifySignedOut();
        stopForeground(true);
        stopSelf();
    }

    private void notifySignedOut() {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            Intent i = new Intent(this, MainActivity.class);
            i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            i.putExtra("screen", "home");
            PendingIntent pi = PendingIntent.getActivity(this, 2, i,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            String text = "Sign in again to keep receiving alerts on this phone.";
            Notification.Builder b = new Notification.Builder(this, CHANNEL)
                    .setContentTitle("Signed out of LabSynch")
                    .setContentText(text)
                    .setStyle(new Notification.BigTextStyle().bigText(
                            "You were signed out of LabSynch (signed out on the site, or the account was changed by an administrator). "
                            + text))
                    .setSmallIcon(R.drawable.ic_stat_bell)
                    .setAutoCancel(true)
                    .setShowWhen(true)
                    .setContentIntent(pi);
            if (android.os.Build.VERSION.SDK_INT < 26) {
                b = b.setPriority(Notification.PRIORITY_HIGH);
            }
            nm.notify(SIGNED_OUT_NOTIF_ID, b.build());
        } catch (Exception ignored) {
        }
    }

    private void ring(String text) {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);

        // Play the user's chosen sound (from the in-app picker). The player
        // holds its own wake lock (setWakeMode) so playback survives the CPU
        // suspending with the screen off; the ring lock below covers the
        // setup and the fallback paths.
        PowerManager.WakeLock ringLock = acquire("labcare:ring", 30000);
        boolean playerOwnsWake = false;
        android.media.MediaPlayer mp = null;
        try {
            mp = android.media.MediaPlayer.create(
                    this, soundResId(), new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                            .build(), 1);
        } catch (Exception ignored) {
        }
        if (mp != null) {
            try {
                mp.setWakeMode(this, PowerManager.PARTIAL_WAKE_LOCK);
                mp.setOnCompletionListener(android.media.MediaPlayer::release);
                mp.start();
                playerOwnsWake = true;
            } catch (Exception e) {
                try { mp.release(); } catch (Exception ignored) { }
                playDefaultRingtone();
            }
        } else {
            playDefaultRingtone();
        }

        Intent i = new Intent(this, MainActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        // Vibrate: ON-notification channel (O+) handles it, this explicit
        // buzz keeps older devices buzzing too and overlaps cleanly.
        buzz();

        Notification.Builder b = new Notification.Builder(this, CHANNEL)
                .setContentTitle("\uD83D\uDD14 LabSynch alert")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setSmallIcon(R.drawable.ic_stat_bell)
                .setAutoCancel(true)
                .setCategory(Notification.CATEGORY_ALARM)
                .setShowWhen(true)
                .setContentIntent(pi)
                // Turn the screen on / ring over the lock screen too. Where
                // the user has not granted full-screen intents (Android 14
                // asks per-app) it degrades to a normal heads-up banner and
                // the sound still rings.
                .setFullScreenIntent(pi, true);
        if (android.os.Build.VERSION.SDK_INT < 26) {
            // heads-up banner pre-O comes from the priority, sound & vibration
            b = b.setPriority(Notification.PRIORITY_MAX)
                 .setVibrate(new long[]{0, 250, 120, 250, 120, 250})
                 .setSound(android.net.Uri.parse(
                         "android.resource://" + getPackageName() + "/" + soundResId()));
        }
        try {
            nm.notify((int) (System.currentTimeMillis() % Integer.MAX_VALUE), b.build());
        } catch (Exception ignored) {
        }

        if (playerOwnsWake) release(ringLock);
        // else: keep the ring lock until its timeout so the default
        // ringtone (which holds no wake lock) finishes playing.
    }

    private void playDefaultRingtone() {
        try {
            RingtoneManager.getRingtone(this,
                    RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)).play();
        } catch (Exception ignored) {
        }
    }

    private PowerManager.WakeLock acquire(String tag, long timeoutMs) {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm == null) return null;
            PowerManager.WakeLock l = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, tag);
            l.setReferenceCounted(false);
            l.acquire(timeoutMs); // always times out — no leak if a release is missed
            return l;
        } catch (Exception e) {
            return null;
        }
    }

    private void release(PowerManager.WakeLock l) {
        try {
            if (l != null && l.isHeld()) l.release();
        } catch (Exception ignored) {
        }
    }

    private void buzz() {
        try {
            android.os.Vibrator vib;
            if (android.os.Build.VERSION.SDK_INT >= 31) {
                android.os.VibratorManager vm = (android.os.VibratorManager)
                        getSystemService(Context.VIBRATOR_MANAGER_SERVICE);
                vib = vm != null ? vm.getDefaultVibrator() : null;
            } else {
                vib = (android.os.Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            }
            if (vib == null || !vib.hasVibrator()) return;
            long[] pattern = {0, 250, 120, 250, 120, 250};
            if (android.os.Build.VERSION.SDK_INT >= 26) {
                vib.vibrate(android.os.VibrationEffect.createWaveform(pattern, -1));
            } else {
                vib.vibrate(pattern, -1);
            }
        } catch (Exception ignored) {
        }
    }

    private int soundResId() {
        int r = prefs.getInt("sound_res", 0);
        if (r != 0) return r;
        switch (prefs.getString("sound", "chime")) {
            case "bell": return R.raw.bell;
            case "beep": return R.raw.beep;
            case "alarm": return R.raw.alarm;
            default: return R.raw.chime;
        }
    }

    private void createChannel() {
        if (android.os.Build.VERSION.SDK_INT >= 26) {
            // IMPORTANCE_HIGH = heads-up banner floating on top of any app.
            // Sound stays null: ring() plays the user's chosen tune itself,
            // so the user never hears a doubled notification ding.
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "LabSynch alerts", NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription("Heads-up bubble + sound + vibration for new LabSynch bell notifications");
            ch.enableVibration(true);
            ch.setVibrationPattern(new long[]{0, 250, 120, 250, 120, 250});
            ch.enableLights(true);
            NotificationManager nm = getSystemService(NotificationManager.class);
            nm.createNotificationChannel(ch);
        }
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        super.onTaskRemoved(rootIntent);
        // Swiping the app away from Recents kills the whole process on many
        // phones (Xiaomi/Oppo/Vivo/Huawei and others). We are still alive at
        // this moment, so restart the relay now and schedule a delayed
        // restart as backup for the aggressive OEMs.
        if (Alarms.wanted(this)) {
            Alarms.poke(this, "START");
            Alarms.schedule(this, Alarms.ACTION_RESTART, 1500);
        }
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (handler != null && poller != null) handler.removeCallbacks(poller);
        handler = null;
        poller = null;
        if (Alarms.wanted(this)) {
            // START_STICKY normally brings us back; OEM killers sometimes
            // swallow the sticky restart, so schedule our own too.
            Alarms.schedule(this, Alarms.ACTION_RESTART, 2500);
            Alarms.schedule(this, Alarms.ACTION_WATCHDOG, 60000);
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
