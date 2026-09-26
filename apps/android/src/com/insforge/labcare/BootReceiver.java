package com.insforge.labcare;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/**
 * Auto-start the alert relay after the phone reboots and after the app is
 * updated (an update kills the process and clears every alarm, so without
 * this the relay would stay down until the app is opened again).
 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (action == null) return;
        if (Intent.ACTION_BOOT_COMPLETED.equals(action)
                || "android.intent.action.QUICKBOOT_POWERON".equals(action)
                || Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) {
            if (Alarms.wanted(context)) {
                Intent s = new Intent(context, AlertRelayService.class);
                s.setAction("START");
                try {
                    if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(s);
                    else context.startService(s);
                } catch (Exception ignored) {
                }
                // Even if that start was refused (rare on these system
                // broadcasts), the watchdog chain resurrects the relay.
                Alarms.schedule(context, Alarms.ACTION_WATCHDOG, 30000);
            }
        }
    }
}
