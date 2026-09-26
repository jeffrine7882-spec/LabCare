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

import org.json.JSONObject;

/**
 * Foreground relay: polls the LabSynch bell endpoint and, on a new unread
 * notification, drops a HEADS-UP bubble over the top of the display, rings
 * the user's chosen sound and vibrates — the phone alerts even when the
 * LabSynch app UI is closed or the screen is off.
 */
public class AlertRelayService extends Service {

    private static final String CHANNEL = "labcare_alerts_v2"; // new id: heads-up settings land on upgrades too
    private Handler handler;
    private Runnable poller;
    private SharedPreferences prefs;
    private long lastId = -1;

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences("labcare", MODE_PRIVATE);
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "STOP".equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }

        String token = prefs.getString("token", "");
        if (token.isEmpty()) {
            stopSelf();
            return START_NOT_STICKY;
        }

        // Persistent notification so Android doesn't kill us when idle.
        Notification n = new Notification.Builder(this, CHANNEL)
                .setContentTitle("LabSynch alerts on")
                .setContentText("Listening for equipment complaints & breakdowns")
                .setSmallIcon(R.drawable.ic_stat_bell)
                .setOngoing(true)
                .build();
        startForeground(1001, n);

        startPolling();
        return START_STICKY;
    }

    private void startPolling() {
        handler = new Handler(Looper.getMainLooper());
        poller = new Runnable() {
            @Override
            public void run() {
                new Thread(() -> pollOnce()).start();
                handler.postDelayed(this, 10000);
            }
        };
        handler.post(poller);
    }

    private void pollOnce() {
        String token = prefs.getString("token", "");
        if (token.isEmpty()) return;
        try {
            String body = httpGet("/api/notifications/ping", token);
            JSONObject j = new JSONObject(body);
            if (j.has("error")) return;
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
        }
    }

    private void ring(String text) {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);

        // play the user's chosen sound (from the in-app picker)
        android.media.MediaPlayer mp = android.media.MediaPlayer.create(
                this, soundResId(), new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                        .build(), 1);
        try {
            if (mp != null) {
                mp.setOnCompletionListener(android.media.MediaPlayer::release);
                mp.start();
            } else {
                RingtoneManager.getRingtone(this,
                        RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)).play();
            }
        } catch (Exception e) {
            try {
                RingtoneManager.getRingtone(this,
                        RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)).play();
            } catch (Exception ignored) {
            }
        }

        Intent i = new Intent(this, MainActivity.class);
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
                .setCategory(Notification.CATEGORY_MESSAGE)
                .setShowWhen(true)
                .setContentIntent(pi);
        if (android.os.Build.VERSION.SDK_INT < 26) {
            // heads-up banner pre-O comes from the priority, sound & vibration
            b = b.setPriority(Notification.PRIORITY_MAX)
                 .setVibrate(new long[]{0, 250, 120, 250, 120, 250})
                 .setSound(android.net.Uri.parse(
                         "android.resource://" + getPackageName() + "/" + soundResId()));
        }
        nm.notify((int) (System.currentTimeMillis() % Integer.MAX_VALUE), b.build());
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

    private String httpGet(String path, String token) throws Exception {
        return Api.get(path, token);
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (handler != null && poller != null) handler.removeCallbacks(poller);
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
