package ru.moexrobot.remote

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.net.http.SslCertificate
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import java.io.ByteArrayInputStream
import java.net.URL
import java.security.cert.CertificateException
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager
import org.json.JSONObject
import java.security.MessageDigest
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate

/**
 * Пульт робота Мосбиржи. Сам робот работает круглосуточно на сервере; приложение показывает его экран
 * (позиции, формации, график, сделки, настройки) и даёт им управлять.
 *
 * Безопасность: сервер подписан собственным сертификатом. Приложение доверяет ему ТОЛЬКО если отпечаток
 * совпадает с отпечатком из кода подключения. В систему телефона никакие сертификаты не устанавливаются.
 */
class MainActivity : Activity() {

    data class Conn(val host: String, val port: Int, val key: String, val fp: String) {
        val base get() = "https://$host:$port/"
    }

    private var web: WebView? = null
    private var conn: Conn? = null
    private var switched = false

    private val prefs by lazy { getSharedPreferences("conn", Context.MODE_PRIVATE) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        createChannel()
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        intent?.data?.toString()?.let { parse(it) }?.let { save(it) }
        conn = load()
        if (conn == null) showSetup(null) else showWeb()
        startLive()
    }

    /** Связь с роботом и в свёрнутом виде (живые цены, уведомления о сделках). */
    private fun startLive() {
        try {
            val i = Intent(this, LiveService::class.java)
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(i) else startService(i)
        } catch (e: Exception) { /* система не дала — работаем, пока приложение открыто */ }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        val c = intent.data?.toString()?.let { parse(it) } ?: return
        save(c); conn = c; showWeb()
    }

    // ------------------------------------------------------------------ код подключения
    private fun parse(s: String): Conn? {
        val m = Regex("""^moexrobot://([^/:\s]+):(\d+)/([A-Za-z0-9_\-]+)/([0-9a-fA-F]{64})$""").find(s.trim()) ?: return null
        val (h, p, k, f) = m.destructured
        return Conn(h, p.toInt(), k, f.lowercase())
    }

    private fun save(c: Conn) = prefs.edit().putString("host", c.host).putInt("port", c.port)
        .putString("key", c.key).putString("fp", c.fp).apply()

    // второй сервер пары: приложение узнаёт его код у основного и переключается, если основной не отвечает
    private fun saveBackup(code: String) = prefs.edit().putString("backup", code).apply()
    private fun backup(): Conn? = prefs.getString("backup", null)?.let { parse(it) }
    private fun primary(): Conn? = load()

    private fun load(): Conn? {
        val h = prefs.getString("host", null) ?: return null
        return Conn(h, prefs.getInt("port", 8443), prefs.getString("key", "") ?: "", prefs.getString("fp", "") ?: "")
    }

    // ------------------------------------------------------------------ экран подключения
    private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

    private fun showSetup(err: String?) {
        web?.let { (it.parent as? ViewGroup)?.removeView(it); it.destroy() }
        web = null
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(22), dp(40), dp(22), dp(24))
        }
        fun text(s: String, size: Float, color: Int, bold: Boolean = false) = TextView(this).apply {
            text = s; textSize = size; setTextColor(color)
            if (bold) typeface = Typeface.DEFAULT_BOLD
            setPadding(0, dp(6), 0, dp(6))
        }
        fun button(s: String, bg: Int, onClick: () -> Unit) = Button(this).apply {
            text = s; isAllCaps = false; textSize = 16f; setTextColor(Color.WHITE)
            setBackgroundColor(bg)
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52)).apply { topMargin = dp(12) }
            setOnClickListener { onClick() }
        }
        box.addView(text("Робот Мосбиржи", 26f, Color.WHITE, true))
        box.addView(text("Пульт робота: сам робот круглосуточно работает на сервере, а здесь видно его позиции, " +
                "формации, график и сделки, и можно им управлять.", 15f, Color.parseColor("#AEB5C6")))
        box.addView(text("Чтобы подключиться, отсканируйте QR-код, который показала программа на компьютере после " +
                "установки сервера, или вставьте код подключения (строка moexrobot://…).", 15f, Color.parseColor("#AEB5C6")))
        if (err != null) box.addView(text(err, 15f, Color.parseColor("#EF5350")))
        box.addView(button("Сканировать QR-код", Color.parseColor("#2F7CF6")) { scan() })
        val input = EditText(this).apply {
            hint = "moexrobot://адрес:8443/ключ/отпечаток"
            setHintTextColor(Color.parseColor("#5E667A")); setTextColor(Color.WHITE); textSize = 14f
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            minLines = 3; gravity = Gravity.TOP
            setBackgroundColor(Color.parseColor("#1E2536")); setPadding(dp(12), dp(10), dp(12), dp(10))
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(22) }
        }
        box.addView(input)
        box.addView(button("Вставить из буфера", Color.parseColor("#2A3247")) {
            val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            input.setText(cm.primaryClip?.getItemAt(0)?.coerceToText(this)?.toString() ?: "")
        })
        box.addView(button("Подключиться", Color.parseColor("#26A69A")) {
            val c = parse(input.text.toString())
            if (c == null) Toast.makeText(this, "Код не распознан — он начинается с moexrobot://", Toast.LENGTH_LONG).show()
            else { save(c); conn = c; showWeb() }
        })
        setContentView(ScrollView(this).apply { setBackgroundColor(Color.parseColor("#0F1420")); addView(box) })
    }

    private fun scan() {
        val opts = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
        GmsBarcodeScanning.getClient(this, opts).startScan()
            .addOnSuccessListener { b ->
                val c = parse(b.rawValue ?: "")
                if (c == null) showSetup("Это не код подключения робота.") else { save(c); conn = c; showWeb() }
            }
            .addOnFailureListener {
                Toast.makeText(this, "Сканер недоступен на этом телефоне — вставьте код текстом.", Toast.LENGTH_LONG).show()
            }
    }

    // ------------------------------------------------------------------ пульт (веб-экран сервера)
    @SuppressLint("SetJavaScriptEnabled")
    private fun showWeb() {
        val c = conn ?: return showSetup(null)
        val w = WebView(this)
        web?.destroy()
        web = w
        w.setBackgroundColor(Color.parseColor("#0F1420"))
        w.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_NO_CACHE
            setSupportMultipleWindows(false)
            allowFileAccess = false
            allowContentAccess = false
        }
        w.addJavascriptInterface(Bridge(), "Android")
        w.webChromeClient = WebChromeClient()
        w.webViewClient = object : WebViewClient() {
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                val got = fingerprint(error.certificate)
                if (got != null && got == c.fp && Uri.parse(error.url).host == c.host) handler.proceed()
                else {
                    handler.cancel()
                    runOnUiThread { showSetup("Сервер предъявил чужой сертификат — соединение отклонено. " +
                            "Проверьте код подключения (возможно, сервер переустановлен).") }
                }
            }

            override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean {
                val u = req.url
                if (u.host == c.host && u.port == c.port) return false
                if (u.scheme == "moexrobot") { parse(u.toString())?.let { save(it); conn = it; showWeb() }; return true }
                try { startActivity(Intent(Intent.ACTION_VIEW, u)) } catch (_: Exception) {}
                return true      // ссылки Т-Инвестиций и прочие — во внешнем приложении/браузере
            }

            override fun onReceivedError(view: WebView, req: WebResourceRequest, err: WebResourceError) {
                if (!req.isForMainFrame) return
                val other = listOfNotNull(primary(), backup()).firstOrNull { it.host != c.host || it.port != c.port }
                if (other != null && !switched) {          // этот сервер не отвечает — на второй сервер пары
                    switched = true
                    conn = other
                    runOnUiThread { Toast.makeText(this@MainActivity, "Сервер ${c.host} не отвечает — подключаюсь ко второму (${other.host})", Toast.LENGTH_LONG).show(); showWeb() }
                    return
                }
                view.loadDataWithBaseURL(null, offlinePage(err.description.toString()), "text/html", "utf-8", null)
            }
        }
        setContentView(w)
        w.loadUrl(c.base + "#k=" + c.key)
        checkServerVersion(c)
    }

    // ------------------------------------------------------------------ обновление программы сервера
    // В приложение при сборке вкладывается архив программы сервера (тот же, что ставит установщик на компьютере).
    // Если на сервере версия старее — приложение предлагает обновить её одной кнопкой (по ключу из кода подключения).
    private val bundle: ByteArray? by lazy {
        try { assets.open("server_bundle.zip").use { it.readBytes() } } catch (_: Exception) { null }
    }

    private fun bundleVersion(): String {
        val b = bundle ?: return ""
        java.util.zip.ZipInputStream(ByteArrayInputStream(b)).use { z ->
            while (true) {
                val e = z.nextEntry ?: break
                if (e.name == "version.txt") return z.readBytes().toString(Charsets.UTF_8).trim()
            }
        }
        return ""
    }

    private fun sha(v: String) = v.trim().split(" ").firstOrNull()?.removePrefix("v") ?: ""

    private fun pinned(c: Conn, path: String): HttpsURLConnection {
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}
            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
                val d = MessageDigest.getInstance("SHA-256").digest(chain[0].encoded).joinToString("") { "%02x".format(it) }
                if (d != c.fp) throw CertificateException("чужой сертификат сервера")
            }
            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
        }
        val ctx = SSLContext.getInstance("TLS"); ctx.init(null, arrayOf(tm), null)
        val conn = URL(c.base.trimEnd('/') + path).openConnection() as HttpsURLConnection
        conn.sslSocketFactory = ctx.socketFactory
        conn.hostnameVerifier = HostnameVerifier { _, _ -> true }     // сервер узнаём по отпечатку сертификата
        conn.setRequestProperty("X-Key", c.key)
        conn.connectTimeout = 15000; conn.readTimeout = 60000
        return conn
    }

    private fun serverVersion(c: Conn): String {
        val conn = pinned(c, "/api/state")
        return JSONObject(conn.inputStream.bufferedReader().readText()).optString("version", "")
    }

    private fun checkServerVersion(c: Conn) {
        Thread {
            try {                                       // пара серверов: запомнить код второго
                val pi = JSONObject(pinned(c, "/api/pair_info").inputStream.bufferedReader().readText())
                val code = pi.optString("peer_code", "")
                if (pi.optBoolean("enabled") && parse(code) != null && c.host == primary()?.host) saveBackup(code)
            } catch (_: Exception) {}
        }.start()
        val mine = bundleVersion()
        if (mine.isEmpty()) return
        Thread {
            try {
                val theirs = serverVersion(c)
                if (theirs.isNotEmpty() && sha(theirs) != sha(mine) && prefs.getString("skip_ver", "") != sha(mine)) {
                    runOnUiThread {
                        AlertDialog.Builder(this)
                            .setTitle("Обновить программу на сервере?")
                            .setMessage("На сервере: $theirs\nВ этом приложении: $mine\n\nРобот перезапустится на ~20 секунд; " +
                                    "позиции, настройки и журналы сохранятся.")
                            .setPositiveButton("Обновить") { _, _ -> updateServer() }
                            .setNegativeButton("Не сейчас") { _, _ -> prefs.edit().putString("skip_ver", sha(mine)).apply() }
                            .show()
                    }
                }
            } catch (_: Exception) {}
        }.start()
    }

    private fun updateServer() {
        val c = conn ?: return
        val b = bundle ?: run { toast("В приложении нет программы сервера — установите свежую версию приложения."); return }
        toast("Обновляю сервер…")
        Thread {
            try {
                val body = JSONObject().put("zip", android.util.Base64.encodeToString(b, android.util.Base64.NO_WRAP)).toString()
                val conn = pinned(c, "/api/update")
                conn.requestMethod = "POST"; conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toByteArray()) }
                val code = conn.responseCode
                if (code == 404) {
                    runOnUiThread { AlertDialog.Builder(this).setMessage("Программа на сервере слишком старая и не умеет обновляться с телефона. " +
                            "Обновите её один раз с компьютера: вкладка «Сервер и телефон» → «Установить / обновить сервер». " +
                            "Дальше обновлять можно отсюда.").setPositiveButton("Понятно", null).show() }
                    return@Thread
                }
                if (code != 200) throw Exception("сервер ответил $code: " + (conn.errorStream?.bufferedReader()?.readText() ?: ""))
                runOnUiThread { toast("Сервер обновлён и перезапускается — экран обновится через 20 секунд.") }
                Thread.sleep(20000)
                runOnUiThread { showWeb() }
            } catch (e: Exception) {
                runOnUiThread { toast("Не удалось обновить сервер: ${e.message}") }
            }
        }.start()
    }

    private fun toast(t: String) = Toast.makeText(this, t, Toast.LENGTH_LONG).show()

    private fun offlinePage(why: String) = """
        <html><body style="background:#0f1420;color:#e6e9f0;font-family:sans-serif;padding:32px 22px">
        <h2>Нет связи с сервером робота</h2>
        <p style="color:#aeb5c6">${conn?.host}:${conn?.port} — $why</p>
        <p style="color:#aeb5c6">Проверьте интернет на телефоне. Если не помогает — сервер выключен или перезагружается
        (робот на сервере продолжает работать независимо от телефона).</p>
        <button onclick="Android.retry()" style="width:100%;padding:16px;border:0;border-radius:10px;background:#2f7cf6;color:#fff;font-size:17px">Повторить</button>
        <button onclick="Android.reset()" style="width:100%;margin-top:12px;padding:14px;border:1px solid #283048;border-radius:10px;background:transparent;color:#aeb5c6;font-size:15px">Другой код подключения</button>
        </body></html>""".trimIndent()

    private fun fingerprint(sc: SslCertificate?): String? {
        if (sc == null) return null
        val x509: X509Certificate? = if (Build.VERSION.SDK_INT >= 29) sc.x509Certificate else {
            val der = SslCertificate.saveState(sc).getByteArray("x509-certificate") ?: return null
            CertificateFactory.getInstance("X.509").generateCertificate(ByteArrayInputStream(der)) as X509Certificate
        }
        val d = MessageDigest.getInstance("SHA-256").digest(x509?.encoded ?: return null)
        return d.joinToString("") { "%02x".format(it) }
    }

    inner class Bridge {
        @JavascriptInterface
        fun notify(title: String, text: String) = runOnUiThread { showNotification(title, text) }

        @JavascriptInterface
        fun reset() = runOnUiThread {
            AlertDialog.Builder(this@MainActivity)
                .setMessage("Отключиться от этого сервера и ввести другой код подключения?")
                .setPositiveButton("Да") { _, _ -> prefs.edit().clear().apply(); conn = null; showSetup(null) }
                .setNegativeButton("Отмена", null).show()
        }

        @JavascriptInterface
        fun retry() = runOnUiThread { switched = false; conn = primary() ?: conn; showWeb() }

        @JavascriptInterface
        fun bundleVersion(): String = this@MainActivity.bundleVersion()

        @JavascriptInterface
        fun updateServer() = runOnUiThread {
            AlertDialog.Builder(this@MainActivity)
                .setMessage("Обновить программу на сервере до версии из приложения (${bundleVersion()})?\n\n" +
                        "Робот перезапустится на ~20 секунд; позиции, настройки и журналы сохранятся.")
                .setPositiveButton("Обновить") { _, _ -> this@MainActivity.updateServer() }
                .setNegativeButton("Отмена", null).show()
        }
    }

    // ------------------------------------------------------------------ уведомления
    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("trades", "Сделки робота", NotificationManager.IMPORTANCE_HIGH))
    }

    private fun showNotification(title: String, text: String) {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val n = android.app.Notification.Builder(this, "trades")
            .setSmallIcon(R.drawable.ic_fg)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(android.app.Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(android.app.PendingIntent.getActivity(this, 0,
                Intent(this, MainActivity::class.java), android.app.PendingIntent.FLAG_IMMUTABLE))
            .build()
        getSystemService(NotificationManager::class.java).notify((System.currentTimeMillis() % 100000).toInt(), n)
    }

    // ------------------------------------------------------------------ системное
    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        val w = web
        if (w != null && w.canGoBack()) w.goBack() else super.onBackPressed()
    }

    override fun onResume() { super.onResume(); web?.onResume(); web?.resumeTimers() }
    // web.onPause() не вызываем: в свёрнутом приложении страница продолжает получать цены и сделки
    override fun onPause() { super.onPause() }
    override fun onDestroy() {
        try { stopService(Intent(this, LiveService::class.java)) } catch (e: Exception) {}
        web?.destroy(); super.onDestroy()
    }

    @Suppress("unused")
    private fun hide(v: View) { v.visibility = View.GONE }
}
