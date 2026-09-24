package com.insforge.labcare;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/** Auto-start the alert relay after the phone reboots. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (action == null) return;
        if (Intent.ACTION_BOOT_COMPLETED.equals(action)
                || "android.intent.action.QUICKBOOT_POWERON".equals(action)) {
            if (!context.getSharedPreferences("labcare", Context.MODE_PRIVATE)
                    .getString("token", "").isEmpty()) {
                Intent s = new Intent(context, AlertRelayService.class);
                s.setAction("START");
                try {
                    if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(s);
                    else context.startService(s);
                } catch (Exception ignored) {
                }
            }
        }
    }
}
