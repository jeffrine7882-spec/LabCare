package com.insforge.labcare;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.SystemClock;

/**
 * Self-rearming alarm chains that make the relay ring even when the app is
 * closed. Three chains, all delivered to {@link WatchdogReceiver}:
 *
 *  * WAKE      — every ~12 s. Handler/postDelayed timers freeze the moment
 *                the CPU suspends (screen off), so this alarm is what keeps
 *                the bell checks running while the phone sleeps. Scheduled
 *                with setExactAndAllowWhileIdle, which fires in Doze and
 *                whose firing is exempt from the Android 12+ ban on starting
 *                foreground services from the background.
 *  * WATCHDOG  — every 60 s. Checks the relay's heartbeat; if polls stopped
 *                (process killed by an OEM "battery saver", a crash, …) it
 *                resurrects the relay and re-arms the WAKE chain.
 *  * RESTART   — one-shot delayed restart used by onTaskRemoved()/onDestroy()
 *                and as the setAlarmClock() fallback when a direct
 *                startForegroundService() was refused.
 */
final class Alarms {

    static final String ACTION_WAKE = "com.insforge.labcare.action.WAKE";
    static final String ACTION_WATCHDOG = "com.insforge.labcare.action.WATCHDOG";
    static final String ACTION_RESTART = "com.insforge.labcare.action.RESTART";

    private Alarms() {
    }

    /** True when the relay should be running at all. */
    static boolean wanted(Context c) {
        SharedPreferences p = c.getSharedPreferences("labcare", Context.MODE_PRIVATE);
        return !p.getString("token", "").isEmpty()
                && p.getBoolean("alerts", true)
                && !p.getBoolean("relay_user_stopped", false);
    }

    private static PendingIntent pi(Context c, String action) {
        Intent i = new Intent(c, WatchdogReceiver.class);
        i.setAction(action);
        return PendingIntent.getBroadcast(c, action.hashCode(), i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    /**
     * True when exact alarms are permitted: always before Android 12, and on
     * 12+ while either SCHEDULE_EXACT_ALARM or USE_EXACT_ALARM is held
     * (USE_EXACT_ALARM is granted at install time for this sideloaded APK).
     */
    static boolean canExact(Context c) {
        if (android.os.Build.VERSION.SDK_INT < 31) return true;
        try {
            AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
            return am != null && am.canScheduleExactAlarms();
        } catch (Exception e) {
            return false;
        }
    }

    /** (Re)arm one chain. Exact (Doze-proof) whenever the platform allows it. */
    static void schedule(Context c, String action, long delayMs) {
        try {
            AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
            if (am == null) return;
            PendingIntent p = pi(c, action);
            long at = SystemClock.elapsedRealtime() + delayMs;
            if (canExact(c)) {
                am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, p);
            } else {
                // no exact-alarm permission: still fires while idle, just
                // possibly batched by a few minutes in deep Doze
                am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, p);
            }
        } catch (Exception ignored) {
        }
    }

    static void cancel(Context c, String action) {
        try {
            AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
            if (am != null) am.cancel(pi(c, action));
        } catch (Exception ignored) {
        }
    }

    static void cancelAll(Context c) {
        cancel(c, ACTION_WAKE);
        cancel(c, ACTION_WATCHDOG);
        cancel(c, ACTION_RESTART);
    }

    /**
     * (Re)start the relay service from any state:
     *  * alive relay — the new start intent just refreshes it,
     *  * dead process — startForegroundService() is allowed because we are
     *    called from an alarm (exact alarms are exempt from the Android 12+
     *    background foreground-service-start ban),
     *  * still refused — retry once through setAlarmClock(), which needs no
     *    permission and puts the app on the temporary allowlist when it
     *    fires, so the next start succeeds.
     */
    static void poke(Context c, String action) {
        Intent i = new Intent(c, AlertRelayService.class);
        i.setAction(action);
        try {
            if (android.os.Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
            else c.startService(i);
            return;
        } catch (Exception ignored) {
        }
        try {
            AlarmManager am = (AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
            if (am == null) return;
            PendingIntent p = pi(c, ACTION_RESTART);
            am.setAlarmClock(new AlarmManager.AlarmClockInfo(
                    SystemClock.elapsedRealtime() + 800, p), p);
        } catch (Exception ignored) {
        }
    }
}
