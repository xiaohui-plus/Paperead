package com.paperead.desktop

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.Settings
import androidx.core.content.FileProvider
import app.tauri.annotation.Command
import app.tauri.annotation.InvokeArg
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.JSArray
import app.tauri.plugin.JSObject
import app.tauri.plugin.Plugin
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

@InvokeArg
class CheckUpdateArgs {
    var endpoints: List<String> = emptyList()
}

@InvokeArg
class InstallUpdateArgs {
    var url: String = ""
    var fallbackUrls: List<String> = emptyList()
    var sha256: String = ""
}

/**
 * Android 应用内更新：拉取 android.json → 下载 APK → 调起系统安装器。
 *
 * 系统不允许静默安装，最后一步必须由用户在安装器里确认；Android 8.0+
 * 首次还需要用户允许「安装未知应用」，此时会跳转到系统设置页。
 */
@TauriPlugin
class ApkUpdaterPlugin(private val activity: Activity) : Plugin(activity) {

    @Command
    fun checkUpdate(invoke: Invoke) {
        val args = invoke.parseArgs(CheckUpdateArgs::class.java)
        Thread {
            try {
                val current = currentVersionName()
                var lastError = "无法连接更新服务"
                for (endpoint in args.endpoints) {
                    val payload = try {
                        fetchJson(endpoint)
                    } catch (e: Exception) {
                        lastError = e.message ?: e.javaClass.simpleName
                        continue
                    }
                    val latest = payload.optString("version").trim()
                    if (latest.isEmpty()) {
                        lastError = "更新清单缺少版本号"
                        continue
                    }
                    val fallbacks = payload.optJSONArray("fallback_urls")
                    val fallbackList = JSArray()
                    if (fallbacks != null) {
                        for (i in 0 until fallbacks.length()) {
                            val item = fallbacks.optString(i)
                            if (item.isNotEmpty()) fallbackList.put(item)
                        }
                    }
                    val result = JSObject().apply {
                        put("current", current)
                        put("latest", latest.removePrefix("v"))
                        put("has_update", isNewer(latest, current))
                        put("notes", payload.optString("notes"))
                        put("pub_date", payload.optString("pub_date"))
                        put("url", payload.optString("url"))
                        put("fallback_urls", fallbackList)
                        put("size", payload.optLong("size", 0L))
                        put("sha256", payload.optString("sha256"))
                        put("source", endpoint)
                    }
                    resolveOnUi(invoke, result)
                    return@Thread
                }
                rejectOnUi(invoke, lastError)
            } catch (e: Exception) {
                rejectOnUi(invoke, e.message ?: "检查更新失败")
            }
        }.start()
    }

    @Command
    fun installUpdate(invoke: Invoke) {
        val args = invoke.parseArgs(InstallUpdateArgs::class.java)
        if (args.url.isEmpty()) {
            invoke.reject("缺少安装包下载地址")
            return
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            !activity.packageManager.canRequestPackageInstalls()
        ) {
            try {
                activity.startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:${activity.packageName}"),
                    )
                )
            } catch (_: Exception) {
                // 个别定制系统没有这个设置页，继续走系统安装器时的提示
            }
            invoke.reject("请先在系统设置中允许 Paperead 安装未知应用，然后重新点击更新")
            return
        }

        Thread {
            try {
                val dir = activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
                    ?: activity.cacheDir
                if (!dir.exists()) dir.mkdirs()
                val target = File(dir, "Paperead-update.apk")
                if (target.exists()) target.delete()

                val urls = (listOf(args.url) + args.fallbackUrls).filter { it.isNotEmpty() }
                downloadApk(urls, target)

                if (args.sha256.isNotEmpty()) {
                    val actual = sha256Of(target)
                    if (!actual.equals(args.sha256, ignoreCase = true)) {
                        target.delete()
                        throw IOException("安装包校验失败，请重新下载")
                    }
                }

                activity.runOnUiThread {
                    try {
                        openInstaller(target)
                        val result = JSObject()
                        result.put("status", "installer_opened")
                        invoke.resolve(result)
                    } catch (e: Exception) {
                        invoke.reject("无法打开系统安装器：${e.message ?: "未知错误"}")
                    }
                }
            } catch (e: Exception) {
                rejectOnUi(invoke, e.message ?: "下载安装包失败")
            }
        }.start()
    }

    private fun currentVersionName(): String {
        @Suppress("DEPRECATION")
        val info = activity.packageManager.getPackageInfo(activity.packageName, 0)
        return info.versionName ?: "0"
    }

    private fun fetchJson(endpoint: String): JSONObject {
        val conn = openConnection(endpoint)
        try {
            if (conn.responseCode !in 200..299) {
                throw IOException("HTTP ${conn.responseCode}")
            }
            val body = conn.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() }
            return JSONObject(body)
        } finally {
            conn.disconnect()
        }
    }

    private fun openConnection(endpoint: String): HttpURLConnection {
        val conn = URL(endpoint).openConnection() as HttpURLConnection
        conn.instanceFollowRedirects = true
        conn.connectTimeout = 15000
        conn.readTimeout = 60000
        conn.setRequestProperty("Accept", "application/json, application/octet-stream")
        conn.setRequestProperty("User-Agent", "Paperead-Android-Updater")
        return conn
    }

    private fun downloadApk(urls: List<String>, target: File) {
        var lastError: Exception? = null
        for (url in urls) {
            val conn = try {
                openConnection(url)
            } catch (e: Exception) {
                lastError = e
                continue
            }
            try {
                if (conn.responseCode !in 200..299) {
                    throw IOException("HTTP ${conn.responseCode}")
                }
                conn.inputStream.use { input ->
                    target.outputStream().use { output -> input.copyTo(output) }
                }
                if (target.length() > 0) return
                throw IOException("下载内容为空")
            } catch (e: Exception) {
                lastError = e
                target.delete()
            } finally {
                conn.disconnect()
            }
        }
        throw lastError ?: IOException("下载失败")
    }

    private fun openInstaller(apk: File) {
        val uri = FileProvider.getUriForFile(
            activity,
            "${activity.packageName}.fileprovider",
            apk,
        )
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        activity.startActivity(intent)
    }

    private fun sha256Of(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                digest.update(buffer, 0, read)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun isNewer(latest: String, current: String): Boolean {
        val a = versionParts(latest)
        val b = versionParts(current)
        val size = maxOf(a.size, b.size)
        for (i in 0 until size) {
            val left = a.getOrElse(i) { 0 }
            val right = b.getOrElse(i) { 0 }
            if (left != right) return left > right
        }
        return false
    }

    private fun versionParts(version: String): List<Int> =
        Regex("\\d+").findAll(version).map { it.value.toInt() }.take(4).toList()

    private fun resolveOnUi(invoke: Invoke, data: JSObject) {
        activity.runOnUiThread { invoke.resolve(data) }
    }

    private fun rejectOnUi(invoke: Invoke, message: String) {
        activity.runOnUiThread { invoke.reject(message) }
    }
}
