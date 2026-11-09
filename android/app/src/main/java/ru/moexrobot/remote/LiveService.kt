package ru.moexrobot.remote

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager

/**
 * Держит приложение живым в свёрнутом виде: живые цены и уведомления о сделках продолжают приходить.
 * Пока работает — в шторке висит тихое уведомление «Робот на связи».
 */
class LiveService : Service() {
    private var wl: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("live", "Связь с роботом", NotificationManager.IMPORTANCE_LOW))
        val n = Notification.Builder(this, "live")
            .setSmallIcon(R.drawable.ic_fg)
            .setContentTitle("Робот на связи")
            .setContentText("Живые цены и сделки приходят и в свёрнутом приложении")
            .setOngoing(true)
            .setContentIntent(PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE))
            .build()
        if (Build.VERSION.SDK_INT >= 34) startForeground(7, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else startForeground(7, n)
        if (wl == null) {
            wl = (getSystemService(POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "moexrobot:live").apply { setReferenceCounted(false); acquire() }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        wl?.let { if (it.isHeld) it.release() }
        wl = null
        super.onDestroy()
    }
}
