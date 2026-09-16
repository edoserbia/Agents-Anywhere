package com.agentsanywhere.app.config

import com.agentsanywhere.app.BuildConfig
import com.agentsanywhere.app.api.normalizeServerOrigin

object AppConfig {
    // Debug builds can override the backend in android/local.properties.
    val OFFICIAL_SERVER_URL: String = BuildConfig.OFFICIAL_SERVER_URL
    const val DESKTOP_DOWNLOAD_URL = "https://agents-anywhere.com/download"
    const val UPDATE_DOWNLOAD_URL = "https://modelscope.cn/models/t4wefan/deepseek-harness-desktop/resolve/master/agents-anywhere-2.0.3-release.apk"

    fun isOfficialServer(serverUrl: String): Boolean {
        val officialOrigin = normalizeServerOrigin(OFFICIAL_SERVER_URL) ?: return false
        return normalizeServerOrigin(serverUrl) == officialOrigin
    }
}
