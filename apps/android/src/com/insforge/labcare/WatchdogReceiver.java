package com.insforge.labcare;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.SystemClock;

/**
 * Keeps the alert relay alive and polling no matter what happened to the
 * app: swiped away from Recents, killed by an OEM battery manager, crashed,
 * or simply asleep with the screen off. See {@link Alarms} for the chains.
 */
public class WatchdogReceiver extends BroadcastReceiver {

    /** Heartbeat is considered fresh while the last poll is younger than this. */
    private static final long WAKE_STALE_MS = 14000;
    /** Relay is presumed dead (not merely asleep) after this long without a poll. */
    private static final long DEAD_STALE_MS = 75000;

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (action == null) return;
        if (!Alarms.wanted(context)) return; // signed out / alerts off / user-stopped

        SharedPreferences prefs = context.getSharedPreferences("labcare", Context.MODE_PRIVATE);
        long hb = prefs.getLong("relay_heartbeat", 0);
        long age = hb <= 0 ? Long.MAX_VALUE : SystemClock.elapsedRealtime() - hb;

        if (Alarms.ACTION_WAKE.equals(action)) {
            // CPU awake and the in-service loop healthy? Nothing to do.
            // Asleep (handlers frozen) or dead? Nudge the relay now.
            if (age > WAKE_STALE_MS) Alarms.poke(context, "WAKE");
            Alarms.schedule(context, Alarms.ACTION_WAKE, 12000);
        } else if (Alarms.ACTION_WATCHDOG.equals(action)) {
            if (age > DEAD_STALE_MS) {
                Alarms.poke(context, "START");
                // the wake chain may have been lost with the process — re-arm it
                Alarms.schedule(context, Alarms.ACTION_WAKE, 3000);
            }
            Alarms.schedule(context, Alarms.ACTION_WATCHDOG, 60000);
        } else if (Alarms.ACTION_RESTART.equals(action)) {
            Alarms.poke(context, "START");
            Alarms.schedule(context, Alarms.ACTION_WAKE, 3000);
            Alarms.schedule(context, Alarms.ACTION_WATCHDOG, 60000);
        }
    }
}
