package com.linkhelm.app

import android.Manifest
import android.content.*
import android.content.pm.PackageManager
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import android.net.wifi.p2p.WifiP2pGroup
import android.net.wifi.p2p.WifiP2pInfo
import android.net.wifi.p2p.WifiP2pManager
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import kotlin.concurrent.thread
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.Image
import androidx.compose.ui.res.painterResource
import androidx.compose.material3.TabRowDefaults.tabIndicatorOffset
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.*
import androidx.compose.runtime.Composable

// --- Runtime UI language (i18n) ---
// External UI defaults to English; the user can switch to Chinese in Settings.
// Reading L10nLang.value inside a @Composable subscribes it to recomposition,
// so toggling the language updates every visible surface immediately.
val L10nLang = mutableStateOf("en")  // "en" (default) | "zh"
object L10n {
    fun tr(en: String, zh: String): String = if (L10nLang.value == "zh") zh else en
}

// Dark Theme (Cyber)
val DarkPurpleBg = Color(0xFF101719)
val DarkCardPurple = Color(0xFF1A2427)
val CyberTeal = Color(0xFF68D7CE)
val CyberPurple = Color(0xFF92A7AE)

// Light Theme (Clean)
val LightBg = Color(0xFFF6F9F8)
val LightSurface = Color(0xFFFFFFFF)
val LightPrimaryTeal = Color(0xFF006E67)
val LightSecondary = Color(0xFF476A70)

private val DarkColorScheme = darkColorScheme(
    primary = CyberTeal,
    secondary = CyberPurple,
    background = DarkPurpleBg,
    surface = DarkCardPurple,
    onBackground = Color.White,
    onSurface = Color.White,
    primaryContainer = CyberTeal.copy(alpha = 0.1f),
    onPrimaryContainer = CyberTeal
)

private val LightColorScheme = lightColorScheme(
    primary = LightPrimaryTeal,
    secondary = LightSecondary,
    background = LightBg,
    surface = LightSurface,
    onBackground = Color(0xFF1C1B1F),
    onSurface = Color(0xFF1C1B1F),
    primaryContainer = LightPrimaryTeal.copy(alpha = 0.1f),
    onPrimaryContainer = LightPrimaryTeal
)

@Composable
fun LinkHelmTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit
) {
    val colorScheme = if (darkTheme) DarkColorScheme else LightColorScheme

    MaterialTheme(
        colorScheme = colorScheme,
        // Add typography here if needed
        content = content
    )
}

data class HostInfo(
    val ssid: String,
    val pass: String,
    val ip: String,
    val port: String
)

class MainActivity : ComponentActivity(), WifiP2pManager.ConnectionInfoListener {

    private lateinit var manager: WifiP2pManager
    private lateinit var channel: WifiP2pManager.Channel
    private lateinit var receiver: BroadcastReceiver
    private val intentFilter = IntentFilter()

    private var hostInfoState = mutableStateOf<HostInfo?>(null)
    private var logState = mutableStateListOf<String>()
    // Fix (v1.2, P0-A): HostService now runs in the SAME process and publishes a
    // Compose-observable EndpointState, so reads are always fresh and auto-refresh.
    private var clientIp = mutableStateOf(HostService.endpointState.ip ?: "192.168.49.1")
    private var clientPort = mutableStateOf("8080")
    // v1.5.47: SOCKS5 credentials - auto-fill the identical built-in defaults on both ends (editable when connecting).
    private var clientUser = mutableStateOf(ProxyServer.DEFAULT_SOCKS_USER)
    private var clientPass = mutableStateOf(ProxyServer.DEFAULT_SOCKS_PASS)
    private var selectedBand = mutableIntStateOf(1)
    private var selectedTab = mutableIntStateOf(0)
    private var showSettings = mutableStateOf(false)

    // --- CRITICAL STATES ---
    private var isHostRunning = mutableStateOf(false)
    private var isClientRunning = mutableStateOf(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Apply the saved UI language (default English) before building any UI.
        L10nLang.value = getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
            .getString("language", "en") ?: "en"
        manager = getSystemService(Context.WIFI_P2P_SERVICE) as WifiP2pManager
        channel = manager.initialize(this, mainLooper, null)

        intentFilter.apply {
            addAction(WifiP2pManager.WIFI_P2P_STATE_CHANGED_ACTION)
            addAction(WifiP2pManager.WIFI_P2P_CONNECTION_CHANGED_ACTION)
            addAction(WifiP2pManager.WIFI_P2P_THIS_DEVICE_CHANGED_ACTION)
        }

        // Load saved band preference
        selectedBand.intValue = getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE).getInt("selected_band", 1)

        // beta1.0.93: ensure the FT receiver from app start — the receiver is
        // independent of sharing now, so a client-mode phone (sharing off,
        // connected to the peer's hotspot) is still able to RECEIVE files.
        runCatching { FileTransferLive.sync(this) }

        checkPermissions()

        setContent {
            val isDarkMode = isSystemInDarkTheme()  // Call directly, not in wrapper
            val colorScheme = if (isDarkMode) DarkColorScheme else LightColorScheme

            MaterialTheme(colorScheme = colorScheme) {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    MainScreen()
                }
            }
        }
    }

    @OptIn(ExperimentalMaterial3Api::class)
    @Composable
    fun MainScreen() {
        Scaffold(
            topBar = {
                if (showSettings.value) {
                    CenterAlignedTopAppBar(
                        title = { Text(L10n.tr("Settings", "设置"), fontWeight = FontWeight.Black, fontSize = 18.sp) },
                        navigationIcon = {
                            IconButton(onClick = { showSettings.value = false }) {
                                Icon(Icons.Default.ArrowBack, contentDescription = L10n.tr("Back", "返回"))
                            }
                        },
                        colors = TopAppBarDefaults.centerAlignedTopAppBarColors(
                            containerColor = MaterialTheme.colorScheme.background,
                            titleContentColor = MaterialTheme.colorScheme.primary
                        )
                    )
                } else {
                    CenterAlignedTopAppBar(
                        title = {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Image(
                                    painter = painterResource(id = R.drawable.app_logo),
                                    contentDescription = "App Logo",
                                    modifier = Modifier.size(32.dp).clip(RoundedCornerShape(4.dp))
                                )
                                Spacer(Modifier.width(12.dp))
                                Column {
                                    Text("LINKHELM", fontWeight = FontWeight.Black, fontSize = 18.sp)
                                    Text(
                                        when {
                                            isHostRunning.value -> L10n.tr("Host service running", "分享服务运行中")
                                            isClientRunning.value -> L10n.tr("Client service running", "连接服务运行中")
                                            else -> L10n.tr("Ready, waiting for connection", "就绪，等待连接")
                                        },
                                        fontSize = 12.sp,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant
                                    )
                                }
                            }
                        },
                        actions = {
                            IconButton(onClick = { showSettings.value = true }) {
                                Icon(Icons.Default.Settings, contentDescription = L10n.tr("Settings", "设置"))
                            }
                        },
                        colors = TopAppBarDefaults.centerAlignedTopAppBarColors(
                            containerColor = MaterialTheme.colorScheme.background,
                            titleContentColor = MaterialTheme.colorScheme.primary
                        )
                    )
                }
            }
        ) { padding ->
            // v1.5.44: the settings page can exceed one screen (Wi-Fi card + SOCKS5 account card)
            // and must scroll; the main layout never scrolls (ScrollState active on the settings page only).
            Column(
                modifier = if (showSettings.value) {
                    Modifier.padding(padding).verticalScroll(rememberScrollState())
                } else {
                    Modifier.padding(padding)
                }
            ) {
                if (showSettings.value) {
                    SettingsScreen()
                } else {
                TabRow(
                    selectedTabIndex = selectedTab.intValue,
                    containerColor = MaterialTheme.colorScheme.background,
                    contentColor = MaterialTheme.colorScheme.primary,
                    divider = {},                  // Removes the thin gray line for a cleaner look
                    indicator = { tabPositions ->
                        if (selectedTab.intValue < tabPositions.size) {
                            TabRowDefaults.SecondaryIndicator(
                                modifier = Modifier.tabIndicatorOffset(tabPositions[selectedTab.intValue]),
                                color = MaterialTheme.colorScheme.primary
                            )
                        }
                    }
                ) {
                    // Tab 1: Share (Host)
                    Tab(
                        selected = selectedTab.intValue == 0,
                        onClick = { selectedTab.intValue = 0 },
                        text = {
                            Text(
                                L10n.tr("Share (Host)", "分享 (主机)"),
                                // Use Theme primary color instead of hardcoded CyberTeal
                                color = if (selectedTab.intValue == 0) MaterialTheme.colorScheme.primary else Color.Gray,
                                fontWeight = if (selectedTab.intValue == 0) FontWeight.Bold else FontWeight.Normal
                            )
                        },
                        icon = {
                            Icon(
                                Icons.Default.Share,
                                contentDescription = null,
                                tint = if (selectedTab.intValue == 0) MaterialTheme.colorScheme.primary else Color.Gray
                            )
                        }
                    )

                    // Tab 2: Connect (Client)
                    Tab(
                        selected = selectedTab.intValue == 1,
                        onClick = { selectedTab.intValue = 1 },
                        text = {
                            Text(
                                L10n.tr("Connect", "连接"),
                                color = if (selectedTab.intValue == 1) MaterialTheme.colorScheme.primary else Color.Gray,
                                fontWeight = if (selectedTab.intValue == 1) FontWeight.Bold else FontWeight.Normal
                            )
                        },
                        icon = {
                            Icon(
                                Icons.Default.VpnLock,
                                contentDescription = null,
                                tint = if (selectedTab.intValue == 1) MaterialTheme.colorScheme.primary else Color.Gray
                            )
                        }
                    )
                }

                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(16.dp)
                        .verticalScroll(rememberScrollState()),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    if (selectedTab.intValue == 0) {
                        HostModeView()
                    } else {
                        ClientModeView()
                    }

                    Spacer(modifier = Modifier.height(24.dp))

                    DebugLogSection()

                    // Footer: copyright / license notice + repository link.
                    // The developer log has been removed per policy.
                    Spacer(modifier = Modifier.height(20.dp))
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            L10n.tr(
                                "Built for personal learning and development. Licensed under the MIT License (see LICENSE); for commercial use the author asks for prior contact (jak21117@gmail.com).",
                                "面向个人学习与开发。按 MIT 许可证授权（见 LICENSE）；如需商用，请先联系作者 (jak21117@gmail.com)。"
                            ),
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            textAlign = TextAlign.Center
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                        val uriHandler = LocalUriHandler.current
                        Text(
                            text = "github.com/jak21117-maker/LinkHelm",
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.clickable {
                                uriHandler.openUri("https://github.com/jak21117-maker/LinkHelm")
                            }
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            "© 2026 LinkHelm Developers",
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
                }
            }
        }
    }

    @Composable
    fun SettingsScreen() {
        val prefs = remember { getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE) }
        var ssid by remember { mutableStateOf(prefs.getString("wifi_ssid", HostService.DEFAULT_SSID) ?: HostService.DEFAULT_SSID) }
        // Minimal hardening: no default password — a random one is generated on first use
        // and persisted, instead of shipping the well-known "87654321".
        // v1.5.0: initialize from prefs directly — ensurePassword() here used to
        // OVERWRITE a just-saved custom password that didn't match the old
        // exactly-10-char rule before the user even saw it.
        var password by remember { mutableStateOf(prefs.getString("wifi_password", null) ?: HostCredentials.ensurePassword(this@MainActivity)) }
        var passwordVisible by remember { mutableStateOf(false) }

        // v1.5.0 rate limiting (user contract):
        // - save: min 10s between saves;
        // - random refresh: min 5s between refreshes, and ≥5 refreshes inside
        //   a 60s window are refused until the window slides open again
        //   ("5 or more times within a minute prompts a retry-later hint; waiting 1-2 minutes allows continuing").
        var lastSaveAt by remember { mutableStateOf(0L) }
        var lastRefreshAt by remember { mutableStateOf(0L) }
        val refreshTimestamps = remember { mutableStateListOf<Long>() }

        fun confirmAndRun(title: String, message: String, action: () -> Unit) {
            android.app.AlertDialog.Builder(this@MainActivity)
                .setTitle(title)
                .setMessage(message)
                .setPositiveButton(L10n.tr("OK", "确认")) { _, _ -> action() }
                .setNegativeButton(L10n.tr("Cancel", "取消"), null)
                .show()
        }

        fun restartHostIfRunning() {
            if (isHostRunning.value) {
                handleStopHost()
                Handler(Looper.getMainLooper()).postDelayed({
                    handleStartHost()
                }, 2000)
            }
        }

        // --- Language (UI i18n) ---
        Card(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(
                    L10n.tr("Language", "语言"),
                    fontWeight = FontWeight.Bold,
                    color = MaterialTheme.colorScheme.onSurface
                )
                Spacer(modifier = Modifier.height(8.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    val langEn = L10nLang.value == "en"
                    FilledTonalButton(
                        onClick = {
                            L10nLang.value = "en"
                            prefs.edit().putString("language", "en").apply()
                        },
                        modifier = Modifier.weight(1f),
                        colors = if (langEn) ButtonDefaults.filledTonalButtonColors()
                        else ButtonDefaults.filledTonalButtonColors(
                            containerColor = MaterialTheme.colorScheme.surfaceVariant,
                            contentColor = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    ) { Text("English") }
                    FilledTonalButton(
                        onClick = {
                            L10nLang.value = "zh"
                            prefs.edit().putString("language", "zh").apply()
                        },
                        modifier = Modifier.weight(1f),
                        colors = if (!langEn) ButtonDefaults.filledTonalButtonColors()
                        else ButtonDefaults.filledTonalButtonColors(
                            containerColor = MaterialTheme.colorScheme.surfaceVariant,
                            contentColor = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    ) { Text(L10n.tr("Chinese", "中文")) }
                }
            }
        }

        Card(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(L10n.tr("Wi-Fi Settings", "Wi-Fi 设置"), fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurface)
                Spacer(modifier = Modifier.height(16.dp))

                OutlinedTextField(
                    value = ssid,
                    onValueChange = { ssid = it },
                    label = { Text("SSID") },
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = { Icon(Icons.Default.Wifi, null) },
                    shape = RoundedCornerShape(12.dp),
                    singleLine = true
                )

                Spacer(modifier = Modifier.height(12.dp))

                OutlinedTextField(
                    value = password,
                    onValueChange = { password = it },
                    label = { Text(L10n.tr("Password (8-63 chars, letters and digits required)", "密码（8-63位，须含字母和数字）")) },
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = { Icon(Icons.Default.Lock, null) },
                    trailingIcon = {
                        IconButton(onClick = { passwordVisible = !passwordVisible }) {
                            Icon(
                                if (passwordVisible) Icons.Default.VisibilityOff else Icons.Default.Visibility,
                                contentDescription = if (passwordVisible) L10n.tr("Hide password", "隐藏密码") else L10n.tr("Show password", "显示密码")
                            )
                        }
                    },
                    visualTransformation = if (passwordVisible) VisualTransformation.None else PasswordVisualTransformation(),
                    shape = RoundedCornerShape(12.dp),
                    singleLine = true
                )

                Spacer(modifier = Modifier.height(20.dp))

                Button(
                    onClick = {
                        // v1.5.0: validate BEFORE saving — invalid input must be
                        // refused loudly, never silently regenerated.
                        // v1.5.31: Wi-Fi Direct group names must keep the
                        // spec-mandated "DIRECT-" prefix — auto-complete it so
                        // typing just "LinkHelm" works instead of failing at
                        // createGroup on the device.
                        if (!ssid.trim().startsWith("DIRECT-")) {
                            ssid = "DIRECT-" + ssid.trim().trimStart('-')
                            Toast.makeText(
                                this@MainActivity,
                                L10n.tr("Wi-Fi Direct name must keep the DIRECT- prefix; auto-completed: $ssid", "Wi-Fi Direct 名称需保留 DIRECT- 前缀，已自动补全：$ssid"),
                                Toast.LENGTH_LONG
                            ).show()
                        }
                        if (ssid.isBlank() || ssid.encodeToByteArray().size > 32) {
                            Toast.makeText(this@MainActivity, L10n.tr("SSID must not be empty or exceed 32 bytes", "SSID 不能为空且不能超过 32 字节"), Toast.LENGTH_SHORT).show()
                            return@Button
                        }
                        if (!HostCredentials.isValidPassword(password)) {
                            Toast.makeText(this@MainActivity, L10n.tr("Password must be 8-63 chars with both letters and digits", "密码需 8-63 位且同时包含字母和数字"), Toast.LENGTH_SHORT).show()
                            return@Button
                        }
                        val now = System.currentTimeMillis()
                        val remain = 10_000 - (now - lastSaveAt)
                        if (remain > 0) {
                            Toast.makeText(this@MainActivity, L10n.tr("Saving too frequent. Try again in ${"%.0f".format(remain / 1000.0)} s", "保存太频繁，请 ${"%.0f".format(remain / 1000.0)} 秒后再试"), Toast.LENGTH_SHORT).show()
                            return@Button
                        }
                        confirmAndRun(
                            L10n.tr("Confirm network settings change", "确认更改网络设置"),
                            L10n.tr("The network name or password will be changed${if (isHostRunning.value) " and sharing will restart automatically" else ""}.\nConnected devices must re-enter the new credentials.", "网络名称或密码将被更改${if (isHostRunning.value) "，分享将自动重启" else ""}。\n已连接的设备需要重新输入新凭据。"),
                        ) {
                            lastSaveAt = System.currentTimeMillis()
                            prefs.edit()
                                .putString("wifi_ssid", ssid)
                                .putString("wifi_password", password)
                                .apply()
                            Toast.makeText(this@MainActivity, L10n.tr("Saved", "已保存"), Toast.LENGTH_SHORT).show()
                            restartHostIfRunning()
                            showSettings.value = false
                        }
                    },
                    modifier = Modifier.fillMaxWidth().height(52.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.primary,
                        contentColor = MaterialTheme.colorScheme.background
                    ),
                    shape = RoundedCornerShape(16.dp)
                ) {
                    Icon(Icons.Default.Save, null)
                    Spacer(Modifier.width(8.dp))
                    Text(L10n.tr("Save", "保存"), fontWeight = FontWeight.ExtraBold, letterSpacing = 1.sp)
                }

                Spacer(modifier = Modifier.height(12.dp))

                // v1.5.0: manual random refresh — works while sharing (the host
                // restarts so the new passphrase takes effect). Double-confirm +
                // 5s spacing + sliding 60s-window limit of 5.
                OutlinedButton(
                    onClick = {
                        val now = System.currentTimeMillis()
                        val remain = 5_000 - (now - lastRefreshAt)
                        if (remain > 0) {
                            Toast.makeText(this@MainActivity, L10n.tr("Refresh too frequent. Try again in ${"%.0f".format(remain / 1000.0)} s", "刷新太频繁，请 ${"%.0f".format(remain / 1000.0)} 秒后再试"), Toast.LENGTH_SHORT).show()
                            return@OutlinedButton
                        }
                        refreshTimestamps.removeAll { it < now - 60_000 }
                        if (refreshTimestamps.size >= 5) {
                            val waitS = (refreshTimestamps.first() + 60_000 - now) / 1000 + 1
                            Toast.makeText(
                                this@MainActivity,
                                L10n.tr("Random refresh too frequent (5 times within 1 minute). Retry in about ${waitS.coerceAtLeast(60)} s", "随机刷新过于频繁（1 分钟内已达 5 次），请约 ${waitS.coerceAtLeast(60)} 秒后重试"),
                                Toast.LENGTH_LONG
                            ).show()
                            return@OutlinedButton
                        }
                        confirmAndRun(
                            L10n.tr("Confirm random password refresh", "确认随机刷新密码"),
                            L10n.tr("A new random password will be generated${if (isHostRunning.value) " and sharing will restart automatically" else ""}.\nConnected devices must re-enter the new credentials.", "将生成新的随机密码${if (isHostRunning.value) "，分享将自动重启" else ""}。\n已连接的设备需要重新输入新凭据。"),
                        ) {
                            lastRefreshAt = System.currentTimeMillis()
                            refreshTimestamps.add(System.currentTimeMillis())
                            val generated = HostCredentials.generatePassword()
                            prefs.edit().putString("wifi_password", generated).apply()
                            password = generated
                            passwordVisible = false
                            Toast.makeText(this@MainActivity, L10n.tr("New password generated: $generated", "新密码已生成：$generated"), Toast.LENGTH_LONG).show()
                            restartHostIfRunning()
                        }
                    },
                    modifier = Modifier.fillMaxWidth().height(48.dp),
                    shape = RoundedCornerShape(16.dp)
                ) {
                    Icon(Icons.Default.Refresh, null)
                    Spacer(Modifier.width(8.dp))
                    Text(L10n.tr("Randomize password", "随机刷新密码"), fontWeight = FontWeight.Bold)
                }
            }
        }

        // v1.5.43: SOCKS5 account settings - built-in fixed credentials by default (never randomized);
        // editable here only; 8-13 alphanumeric characters; changes take effect immediately
        // (new connections authenticate with the new credentials); no "edit without effect" issue.
        var socksUser by remember {
            mutableStateOf(prefs.getString("socks_user", ProxyServer.DEFAULT_SOCKS_USER) ?: ProxyServer.DEFAULT_SOCKS_USER)
        }
        var socksPass by remember {
            mutableStateOf(prefs.getString("socks_pass", ProxyServer.DEFAULT_SOCKS_PASS) ?: ProxyServer.DEFAULT_SOCKS_PASS)
        }
        var socksPassVisible by remember { mutableStateOf(false) }
        Spacer(modifier = Modifier.height(16.dp))
        Card(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(L10n.tr("SOCKS5 Account", "SOCKS5 账户"), fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurface)
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    L10n.tr("Clients (desktop / Karing / Streisand, etc.) must use this account and password to connect. ", "客户端（电脑端 / Karing / Streisand 等）连接时必须使用此账号密码。") +
                        L10n.tr("The defaults are fixed and never randomized; change them manually here only (8-13 alphanumeric characters).", "默认值固定不变；只能在此手动修改（8-13 位英文字母和数字），没有随机刷新。"),
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(modifier = Modifier.height(12.dp))

                OutlinedTextField(
                    value = socksUser,
                    onValueChange = { socksUser = it },
                    label = { Text(L10n.tr("Username (8-13 alphanumeric)", "账号（8-13 位字母数字）")) },
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = { Icon(Icons.Default.Person, null) },
                    shape = RoundedCornerShape(12.dp),
                    singleLine = true
                )

                Spacer(modifier = Modifier.height(12.dp))

                OutlinedTextField(
                    value = socksPass,
                    onValueChange = { socksPass = it },
                    label = { Text(L10n.tr("Password (8-13 alphanumeric)", "密码（8-13 位字母数字）")) },
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = { Icon(Icons.Default.Lock, null) },
                    trailingIcon = {
                        IconButton(onClick = { socksPassVisible = !socksPassVisible }) {
                            Icon(
                                imageVector = if (socksPassVisible) Icons.Default.VisibilityOff else Icons.Default.Visibility,
                                contentDescription = if (socksPassVisible) L10n.tr("Hide password", "隐藏密码") else L10n.tr("Show password", "显示密码")
                            )
                        }
                    },
                    shape = RoundedCornerShape(12.dp),
                    visualTransformation = if (socksPassVisible) VisualTransformation.None else PasswordVisualTransformation(),
                    singleLine = true
                )

                Spacer(modifier = Modifier.height(16.dp))

                Button(
                    onClick = {
                        val u = socksUser.trim()
                        val p = socksPass.trim()
                        if (!ProxyServer.SOCKS_CREDENTIAL_REGEX.matches(u) ||
                            !ProxyServer.SOCKS_CREDENTIAL_REGEX.matches(p)
                        ) {
                            Toast.makeText(
                                this@MainActivity,
                                L10n.tr("Username/password must be 8-13 alphanumeric characters", "账号/密码需 8-13 位英文字母和数字组合"),
                                Toast.LENGTH_LONG
                            ).show()
                            return@Button
                        }
                        confirmAndRun(
                            L10n.tr("Confirm SOCKS5 account change", "确认更改 SOCKS5 账户"),
                            L10n.tr("Changes take effect immediately (new connections authenticate with the new credentials).\n", "更改后立即生效（新连接按新凭据认证）。\n") +
                                L10n.tr("All clients (desktop / Karing / Streisand) must reconnect with the new credentials.", "所有客户端（电脑端 / Karing / Streisand）需同步使用新凭据重新连接。")
                        ) {
                            prefs.edit()
                                .putString("socks_user", u)
                                .putString("socks_pass", p)
                                .apply()
                            // Takes effect immediately while sharing is running; otherwise read from prefs on next start.
                            HostService.applySocksAuthLive(u, p)
                            socksUser = u
                            socksPass = p
                            Toast.makeText(this@MainActivity, L10n.tr("SOCKS5 account updated, effective immediately", "SOCKS5 账户已更新，立即生效"), Toast.LENGTH_LONG).show()
                        }
                    },
                    modifier = Modifier.fillMaxWidth().height(52.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.primary,
                        contentColor = MaterialTheme.colorScheme.background
                    ),
                    shape = RoundedCornerShape(16.dp)
                ) {
                    Icon(Icons.Default.Save, null)
                    Spacer(Modifier.width(8.dp))
                    Text(L10n.tr("Save Account", "保存账户"), fontWeight = FontWeight.ExtraBold, letterSpacing = 1.sp)
                }
            }
        }
    }

    @Composable
    fun HostModeView() {
        Text(L10n.tr("Share your network", "分享你的网络"), modifier = Modifier.fillMaxWidth(), fontSize = 24.sp, fontWeight = FontWeight.Bold)
        Text(L10n.tr("Use this phone as a host to provide network access for other devices.", "把这台手机作为主机，为其他设备提供网络。"), modifier = Modifier.fillMaxWidth(), color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(modifier = Modifier.height(20.dp))
        StatusCard(
            title = L10n.tr("Hotspot sharing", "热点分享"),
            isActive = isHostRunning.value,
            activeColor = Color(0xFF4CAF50),
            icon = Icons.Default.CellTower
        )

        Spacer(modifier = Modifier.height(16.dp))

        SetupSteps(listOf(
            L10n.tr("Turn on mobile data on this phone.", "打开本手机的移动数据。"),
            L10n.tr("Tap Start Sharing, then copy the network and proxy info below.", "点击启动分享，然后复制下方的网络与代理信息。"),
            L10n.tr("Join that Wi-Fi on every client and connect through this app.", "在每台客户端上加入该 Wi-Fi，并通过本应用连接。")
        ))
        Spacer(modifier = Modifier.height(16.dp))

        if (isClientRunning.value) {
            ConflictCard(message = L10n.tr("The connect service is running; sharing cannot be enabled at the same time.", "连接功能正在运行，无法同时开启分享。"))
        }

        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            // FIXED: Uses the theme surface color instead of hardcoded CardPurple
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(L10n.tr("Wi-Fi Settings", "Wi-Fi 设置"), fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurface)
                Spacer(modifier = Modifier.height(8.dp))

                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(L10n.tr("Wi-Fi band: ", "Wi-Fi 频段："), modifier = Modifier.weight(1f), color = MaterialTheme.colorScheme.onSurface)
                    FilterChip(
                        selected = selectedBand.intValue == 1,
                        onClick = { 
                            selectedBand.intValue = 1 
                            getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE).edit().putInt("selected_band", 1).apply()
                        },
                        label = { Text("2.4 GHz") },
                        enabled = !isHostRunning.value && !isClientRunning.value,
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = MaterialTheme.colorScheme.primary,
                            selectedLabelColor = MaterialTheme.colorScheme.background, // Adapts to Light or Dark
                            labelColor = MaterialTheme.colorScheme.onSurface      // Adapts to Black or White text
                        )
                    )
                    Spacer(modifier = Modifier.width(8.dp))
                    FilterChip(
                        selected = selectedBand.intValue == 2,
                        onClick = { 
                            selectedBand.intValue = 2 
                            getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE).edit().putInt("selected_band", 2).apply()
                        },
                        label = { Text("5 GHz") },
                        enabled = !isHostRunning.value && !isClientRunning.value,
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = MaterialTheme.colorScheme.primary,
                            selectedLabelColor = MaterialTheme.colorScheme.background, // Adapts to Light or Dark
                            labelColor = MaterialTheme.colorScheme.onSurface      // Adapts to Black or White text
                        )
                    )
                }

                Spacer(modifier = Modifier.height(16.dp))

                if (!isHostRunning.value) {
                    Button(
                        onClick = { handleStartHost() },
                        modifier = Modifier.fillMaxWidth().height(52.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = MaterialTheme.colorScheme.primary,    // Dynamic
                            contentColor = MaterialTheme.colorScheme.background    // Dynamic
                        ),
                        shape = RoundedCornerShape(16.dp),
                        enabled = !isClientRunning.value
                    ) {
                        Icon(Icons.Default.PlayArrow, null)
                        Spacer(Modifier.width(8.dp))
                        Text(L10n.tr("Start Sharing", "启动分享"), fontWeight = FontWeight.ExtraBold, letterSpacing = 1.sp)
                    }
                } else {
                    Button(
                        onClick = { handleStopHost() },
                        modifier = Modifier.fillMaxWidth().height(52.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = Color(0xFFB00020), // Standard Red
                            contentColor = Color.White
                        ),
                        shape = RoundedCornerShape(16.dp)
                    ) {
                        Icon(Icons.Default.Stop, null)
                        Spacer(Modifier.width(8.dp))
                        Text(L10n.tr("Stop Sharing", "停止分享"), fontWeight = FontWeight.ExtraBold)
                    }
                }
            }
        }

        // Host Info Card (Update for contrast)
        AnimatedVisibility(visible = hostInfoState.value != null) {
            hostInfoState.value?.let { info ->
                // Fix (v1.2, P0-A): observe the service-published endpoint state directly.
                // Any status change (WAITING_FOR_IP → BINDING → SOCKS_READY) recomposes
                // this card automatically; "Host service running" no longer implies the
                // proxy port is ready.
                val endpoint = HostService.observableEndpoint.value
                val displayIp = endpoint.ip
                    ?: (if (isHostRunning.value) L10n.tr("Pending (expected 192.168.49.1, for reference only)", "待分配（预期 192.168.49.1，仅供参考）") else info.ip)
                val displayPort = if (endpoint.port > 0) endpoint.port.toString() else info.port
                Spacer(modifier = Modifier.height(16.dp))
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(
                        containerColor = if (isSystemInDarkTheme()) Color(0xFF1B2E1C) else Color(0xFFE8F5E9)
                    ),
                    border = BorderStroke(1.dp, Color(0xFF4CAF50))
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Default.Info, contentDescription = null, tint = Color(0xFF4CAF50))
                            Spacer(modifier = Modifier.width(8.dp))
                            Text(L10n.tr("Client connection info", "客户端连接信息"), fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurface)
                        }
                        Divider(modifier = Modifier.padding(vertical = 8.dp), color = Color(0xFF4CAF50).copy(alpha = 0.3f))
                        InfoRow(label = "SSID", value = info.ssid)
                        InfoRow(label = L10n.tr("Password", "密码"), value = info.pass)
                        InfoRow(label = L10n.tr("Proxy IP", "代理 IP"), value = displayIp)
                        InfoRow(label = L10n.tr("Proxy Port", "代理端口"), value = displayPort)
                        // v1.5.4: show the host app version so the phone and the
                        // Windows EXE can be paired/verified at a glance in the
                        // field (matches LinkHelm Desktop v1.5.4).
                        InfoRow(
                            label = L10n.tr("Local version", "本机版本"),
                            value = "v" + BuildConfig.VERSION_NAME + L10n.tr(" (recommended with LinkHelm Desktop)", "（建议与 LinkHelm Desktop 配合使用）")
                        )
                        // v1.4.3: UDP self-check result (auto-detected once SOCKS is ready, non-fatal)
                        // v1.4.4: disclose probing behavior + provide an off switch (review advice: users must know and control it)
                        endpoint.udpStatus?.let {
                            InfoRow(label = L10n.tr("UDP Probe", "UDP 检测"), value = it)
                        }
                        endpoint.udpStatus?.let {
                            val ctx = LocalContext.current
                            var autoProbeOn by remember {
                                mutableStateOf(
                                    ctx.getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
                                        .getBoolean("udp_auto_probe", true)
                                )
                            }
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    L10n.tr("Auto probe sends DNS queries via the local UDP relay using the candidate pool (119.29.29.29 -> 223.5.5.5 -> 114.114.114.114) for connectivity diagnostics only; it does not affect actual forwarding.", "自动检测会经本机 UDP 中继按候选池（119.29.29.29 → 223.5.5.5 → 114.114.114.114）发送 DNS 查询，仅用于连通性诊断，不影响实际转发。"),
                                    fontSize = 11.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.weight(1f)
                                )
                                TextButton(onClick = {
                                    autoProbeOn = !autoProbeOn
                                    ctx.getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
                                        .edit().putBoolean("udp_auto_probe", autoProbeOn).apply()
                                    if (!autoProbeOn) {
                                        HostService.publishUdpStatus(L10n.tr("Auto probe disabled", "自动检测已关闭"))
                                    }
                                }) {
                                    Text(
                                        if (autoProbeOn) L10n.tr("Disable auto probe", "关闭自动检测") else L10n.tr("Enable auto probe (takes effect on next share)", "开启自动检测（下次分享生效）"),
                                        fontSize = 12.sp
                                    )
                                }
                            }
                        }
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(
                            text = buildString {
                                append("SOCKS5: ")
                                append(
                                    when {
                                        endpoint.status == HostService.STATUS_SOCKS_READY ->
                                            L10n.tr("Listening on ${endpoint.ip}:${endpoint.port} (connectable)", "✓ 已监听 ${endpoint.ip}:${endpoint.port}（可连接）")
                                        endpoint.status == HostService.STATUS_BINDING -> L10n.tr("Binding...", "绑定中…")
                                        endpoint.status == HostService.STATUS_WAITING_FOR_IP -> L10n.tr("Waiting for P2P interface IP...", "等待 P2P 接口 IP…")
                                        endpoint.status == HostService.STATUS_GROUP_CREATING -> L10n.tr("Creating Wi-Fi Direct group...", "创建 Wi-Fi Direct 群组…")
                                        endpoint.status == HostService.STATUS_GROUP_READY -> L10n.tr("Group ready, acquiring interface IP...", "群组就绪，获取接口 IP…")
                                        endpoint.status.startsWith("FAILED") -> "✗ ${endpoint.status.removePrefix("FAILED:")}"
                                        else -> L10n.tr("Not started", "未启动")
                                    }
                                )
                                endpoint.reason?.let { append("（$it）") }
                            },
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        if (isHostRunning.value && endpoint.status != HostService.STATUS_SOCKS_READY) {
                            Spacer(modifier = Modifier.height(8.dp))
                            TextButton(onClick = {
                                val intent = Intent(this@MainActivity, HostService::class.java).apply { action = "REDETECT" }
                                startService(intent)
                            }) {
                                Text(L10n.tr("Re-detect P2P IP", "重新检测 P2P IP"), fontSize = 13.sp)
                            }
                        }
                        // v1.4.8: Turbo mode - manual toggle at the bottom of the first section (off by default).
                        // Shows an "enable recommended" hint with more than 3 real downstream devices; a dialog
                        // confirms the extra power draw; changes apply on the next share (thread pool built at bind time).
                        val ctxForTurbo = LocalContext.current
                        var turboOn by remember {
                            mutableStateOf(
                                ctxForTurbo.getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
                                    .getBoolean("turbo_mode", false)
                            )
                        }
                        val realDeviceCount = HostService.observableEndpoint.value.clients.size
                        Spacer(modifier = Modifier.height(8.dp))
                        Divider(color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
                        Spacer(modifier = Modifier.height(6.dp))
                        Column {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Column(modifier = Modifier.weight(1f)) {
                                    Text(
                                        L10n.tr("Turbo mode (multi-device)", "强力模式（多设备）"),
                                        fontWeight = FontWeight.Bold,
                                        color = MaterialTheme.colorScheme.onSurface
                                    )
                                    // beta1.0.3: keep the description short —
                                    // more sessions for many devices, more battery.
                                    Text(
                                        L10n.tr("Raises the session cap for multiple devices (uses more battery)", "提升多设备场景的会话数上限（更耗电）"),
                                        fontSize = 11.sp,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant
                                    )
                                }
                                TextButton(onClick = {
                                    if (!turboOn) {
                                        android.app.AlertDialog.Builder(ctxForTurbo)
                                            .setTitle(L10n.tr("Enable turbo mode?", "开启强力模式？"))
                                            .setMessage(
                                                L10n.tr("Raises the session cap to improve stability when several devices share the connection, but uses more battery.\n\nTakes effect immediately; no sharing restart needed.", "提升会话数上限，多台设备同时使用更稳定，但更耗电。\n\n改动立即生效，无需重启分享。")
                                            )
                                            .setPositiveButton(L10n.tr("Enable", "开启")) { _, _ ->
                                                turboOn = true
                                                ctxForTurbo.getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
                                                    .edit().putBoolean("turbo_mode", true).apply()
                                                // v1.5.0: resize the RUNNING pool now.
                                                HostService.applyTurboModeLive(true)
                                            }
                                            .setNegativeButton(L10n.tr("Cancel", "取消"), null)
                                            .show()
                                    } else {
                                        turboOn = false
                                        ctxForTurbo.getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
                                            .edit().putBoolean("turbo_mode", false).apply()
                                        // v1.5.0: resize the RUNNING pool now.
                                        HostService.applyTurboModeLive(false)
                                    }
                                }) {
                                    Text(
                                        if (turboOn) L10n.tr("Off (immediate)", "关闭（立即生效）") else L10n.tr("Enable turbo mode", "开启强力模式"),
                                        fontSize = 12.sp,
                                        color = if (turboOn) MaterialTheme.colorScheme.primary
                                        else Color(0xFFE65100)
                                    )
                                }
                            }
                            // beta1.0.3: with 3+ downstream devices (host excluded),
                            // nudge the user toward turbo mode — its 1400-session
                            // shared budget is what multi-device hosts need.
                            if (realDeviceCount >= 3 && !turboOn) {
                                Spacer(modifier = Modifier.height(2.dp))
                                Text(
                                    L10n.tr(
                                        "${realDeviceCount} devices connected (excluding this host) — turbo mode recommended",
                                        "已连接 ${realDeviceCount} 台设备（不含本机）— 建议开启强力模式"
                                    ),
                                    fontSize = 11.sp,
                                    color = Color(0xFFE65100)
                                )
                            }
                            // beta1.0.51: the old "compatibility micro-kernel"
                            // toggle is RETIRED — multi-port UDP source tracking
                            // and per-target precise replies are now ALWAYS ON.
                            // They used to sit behind this switch, which nobody
                            // ever enabled, so socket-rotating clients
                            // (Karing/Streisand pools, power-saving rebuilds)
                            // silently lost UDP ("check OK, game gets nothing").
                            Spacer(modifier = Modifier.height(6.dp))
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Column(modifier = Modifier.weight(1f)) {
                                    Text(
                                        L10n.tr("UDP multi-port compatibility: always on", "UDP 多源端口兼容：默认开启"),
                                        fontWeight = FontWeight.Bold,
                                        color = MaterialTheme.colorScheme.onSurface
                                    )
                                    Text(
                                        L10n.tr("Source-port rotation from clients such as Karing/Streisand is tracked per target and answered precisely — no switch needed", "Karing/Streisand 等客户端的多源端口轮换按目标精确跟踪回发，无需手动开关"),
                                        fontSize = 11.sp,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }

        // v1.5.45 (beta1.0.1): file-transfer trial entry — single file <= 1 GB
        // over the Wi-Fi Direct link only (never the SOCKS5 internet exit).
        Spacer(modifier = Modifier.height(20.dp))
        FileTransferCard()

        // v1.4.1: iPhone proxy-free entry moved to the bottom of the share page; tap to expand/collapse the guide
        Spacer(modifier = Modifier.height(20.dp))
        var showIphoneGuide by remember { mutableStateOf(false) }
        Card(
            modifier = Modifier.fillMaxWidth().clickable { showIphoneGuide = !showIphoneGuide },
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
        ) {
            Row(
                modifier = Modifier.padding(14.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    Icons.Default.PhoneIphone,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary
                )
                Spacer(modifier = Modifier.width(12.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(L10n.tr("iOS proxy-free networking (no app)", "iOS 免APP联网"), fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurface)
                    Text(
                        L10n.tr("No VPN needed: browse via the system Wi-Fi manual HTTP proxy - tap to ${if (showIphoneGuide) "collapse" else "expand"}", "无需 VPN，用系统 Wi-Fi 手动 HTTP 代理上网 · 点击${if (showIphoneGuide) "收起" else "展开"}"),
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Icon(
                    if (showIphoneGuide) Icons.Default.KeyboardArrowUp else Icons.Default.KeyboardArrowDown,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
        AnimatedVisibility(visible = showIphoneGuide) {
            Column { Spacer(modifier = Modifier.height(12.dp)); IphoneShareView() }
        }

        // v1.4.5: connected-devices entry moved to the very bottom of the share page, using the same
        // tap-to-expand/collapse pattern as iPhone proxy-free - keeps the main flow uncluttered.
        Spacer(modifier = Modifier.height(20.dp))
        var showDevices by remember { mutableStateOf(false) }
        val endpointNow = HostService.observableEndpoint.value
        val activeCount = endpointNow.clients.count { !it.blocked && it.activeConnections > 0 }
        Card(
            modifier = Modifier.fillMaxWidth().clickable { showDevices = !showDevices },
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
        ) {
            Row(
                modifier = Modifier.padding(14.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    Icons.Default.Devices,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary
                )
                Spacer(modifier = Modifier.width(12.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(L10n.tr("Connected devices", "已连接设备"), fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurface)
                    Text(
                        if (isHostRunning.value) {
                            if (activeCount > 0) L10n.tr("$activeCount device(s) online - tap to ${if (showDevices) "collapse" else "expand"}", "$activeCount 台设备在线 · 点击${if (showDevices) "收起" else "展开"}")
                            else L10n.tr("No devices online - tap to ${if (showDevices) "collapse" else "expand"}", "暂无设备在线 · 点击${if (showDevices) "收起" else "展开"}")
                        } else {
                            L10n.tr("Start sharing to view and manage connected devices here - tap to ${if (showDevices) "collapse" else "expand"}", "启动分享后可在此查看并管理接入设备 · 点击${if (showDevices) "收起" else "展开"}")
                        },
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Icon(
                    if (showDevices) Icons.Default.KeyboardArrowUp else Icons.Default.KeyboardArrowDown,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
        AnimatedVisibility(visible = showDevices) {
            Column { Spacer(modifier = Modifier.height(12.dp)); ConnectedDevicesCard(endpointNow) }
        }
    }

    /**
     * v1.4.4 - Downstream device roster: shows devices that joined via the SOCKS5/HTTP proxy in this session,
     * with one-tap Disconnect (closes all connections immediately and blocks reconnect until sharing restarts).
     * Data comes from the HostService Client-Monitor thread (local in-memory snapshot refresh, no network handshake;
     * since v1.4.7 it no longer shows 127.x loopback self-tests or phantom entries of the 192.168.49.1 gateway itself).
     */
    @Composable
    fun ConnectedDevicesCard(endpoint: HostService.Companion.EndpointState) {
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.Devices, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
                    Spacer(modifier = Modifier.width(8.dp))
                    Text(L10n.tr("Connected devices", "已连接设备"), fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Spacer(modifier = Modifier.weight(1f))
                    Text(L10n.tr("Auto-refresh (real downstream devices only)", "自动刷新（仅显示真实下游设备）"), fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(modifier = Modifier.height(8.dp))
                val clients = endpoint.clients
                if (clients.isEmpty()) {
                    Text(
                        L10n.tr("No devices connected. Devices that join via the SOCKS5 proxy (Connect page / desktop) or the HTTP proxy (iOS proxy-free) will appear here.", "暂无设备连接。设备经 SOCKS5 代理（连接页 / 电脑端）或 HTTP 代理（iOS 免APP联网）接入后会显示在这里。"),
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                } else {
                    clients.forEach { device ->
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)
                        ) {
                            Column(modifier = Modifier.weight(1f)) {
                                Text(device.ip, fontSize = 13.sp, fontWeight = FontWeight.Medium)
                                Text(
                                    when {
                                        device.blocked -> L10n.tr("Disconnected and reconnection blocked (restored after restarting sharing)", "已断开并阻止重连（重启分享后恢复）")
                                        device.activeConnections > 0 -> L10n.tr("${device.activeConnections} active connection(s)", "${device.activeConnections} 个活动连接")
                                        else -> L10n.tr("Idle", "空闲")
                                    },
                                    fontSize = 11.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                            if (!device.blocked) {
                                TextButton(onClick = {
                                    val intent = Intent(this@MainActivity, HostService::class.java).apply {
                                        action = "KICK_CLIENT"
                                        putExtra("ip", device.ip)
                                    }
                                    startService(intent)
                                }) {
                                    Text(L10n.tr("Disconnect", "断开"), fontSize = 13.sp, color = Color(0xFFB00020))
                                }
                            }
                        }
                    }
                    Text(
                        L10n.tr("Disconnect immediately closes all TCP/UDP connections of that device and blocks reconnection; all devices can rejoin after sharing restarts.", "「断开」立即关闭该设备的全部 TCP/UDP 连接并禁止重连；重启分享后所有设备恢复可接入。"),
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }
    }

    /**
     * v1.4.1 - iPhone proxy-free setup guide (expandable section at the bottom of the share page).
     * Read-only display: reads HostService state only; never creates/removes groups or starts services.
     */
    @Composable
    fun IphoneShareView() {
        val endpoint = HostService.observableEndpoint.value
        Text(
            L10n.tr("No app or VPN required: iPhone joins the shared network via the built-in Wi-Fi manual HTTP proxy.", "无需安装 App、无需 VPN：iPhone 用系统自带的 Wi-Fi 手动 HTTP 代理接入本机分享网络。"),
            modifier = Modifier.fillMaxWidth(),
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(modifier = Modifier.height(16.dp))

        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(
                containerColor = if (isSystemInDarkTheme()) Color(0xFF1B2E1C) else Color(0xFFE8F5E9)
            ),
            border = BorderStroke(1.dp, Color(0xFF4CAF50))
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.PhoneIphone, contentDescription = null, tint = Color(0xFF4CAF50))
                    Spacer(modifier = Modifier.width(8.dp))
                    Text(L10n.tr("Module status", "模块状态"), fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurface)
                }
                Divider(modifier = Modifier.padding(vertical = 8.dp), color = Color(0xFF4CAF50).copy(alpha = 0.3f))
                InfoRow(label = L10n.tr("SOCKS5 (desktop/Android)", "SOCKS5 (电脑/安卓)"), value = if (endpoint.port > 0) "${endpoint.ip}:${endpoint.port}" else L10n.tr("Not started", "未启动"))
                InfoRow(
                    label = L10n.tr("HTTP proxy (iPhone, no app needed)", "HTTP 代理 (iPhone 免装应用)"),
                    value = if (endpoint.httpPort > 0) "${endpoint.ip}:${endpoint.httpPort}  " + L10n.tr("Ready", "✓ 已就绪") else L10n.tr("Not started (starts automatically with Share)", "未启动（启动 Share 后自动开启）")
                )
                Spacer(modifier = Modifier.height(6.dp))
                Text(
                    // beta1.0.3: keep HTTP, but set expectations — SOCKS5 is the
                    // main distribution channel; HTTP carries only a small slice.
                    L10n.tr("HTTP carries only a small slice of this app's traffic distribution (about 1/10); the main channel is SOCKS5.", "HTTP 仅占本应用流量分发的一小部分（约 1/10），主通道为 SOCKS5。"),
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    L10n.tr("Note: the HTTP proxy carries HTTP/HTTPS traffic only (browser, WeChat, most apps). UDP traffic such as games will not pass on either iPhone or another Android - this is expected; use the Connect page or desktop sharing instead.", "说明：HTTP 代理只承载 HTTP/HTTPS 流量（浏览器、微信、绝大多数 App）。无论是 iPhone 还是另一部安卓，游戏等 UDP 流量都走不通——这属于正常现象，请改用「连接」页或电脑端分享。"),
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }

        Spacer(modifier = Modifier.height(16.dp))
        Text(L10n.tr("iPhone setup steps (no hotspot toggle needed)", "iPhone 连接步骤（无需热点开关）"), fontWeight = FontWeight.Bold, fontSize = 16.sp)
        Spacer(modifier = Modifier.height(8.dp))
        val ip = endpoint.ip ?: "192.168.49.1"
        val httpPort = if (endpoint.httpPort > 0) endpoint.httpPort else HostService.HTTP_PROXY_PORT
        val steps = listOf(
            L10n.tr("1. Start the sharing service on the Share page first (keep the system hotspot toggle off).", "1. 本机先在 Share 页启动分享服务（保持系统热点开关关闭）。"),
            L10n.tr("2. iPhone Settings -> Wi-Fi -> find the DIRECT-xx hotspot and connect with the password.", "2. iPhone 设置 → 无线局域网 → 找到 DIRECT-xx 热点并输入密码连接。"),
            L10n.tr("3. Tap the (i) icon on that network -> scroll to Configure Proxy -> Manual.", "3. 点击该网络右侧 (i) → 拉到底部「配置代理」→ 选择「手动」。"),
            L10n.tr("4. Enter $ip as the server and $httpPort as the port, then save.", "4. 服务器填 $ip，端口填 $httpPort，存储。"),
            L10n.tr("5. Open Safari and visit any site to verify (enable Airplane mode first, then Wi-Fi only, to avoid cellular interference).", "5. 打开 Safari 访问任意网站验证（建议先开飞行模式再仅开 Wi-Fi，避免蜂窝干扰判断）。"),
            L10n.tr("6. Android devices need nothing installed either: long-press the Wi-Fi -> Modify network -> Advanced -> Manual proxy, same address and port.", "6. Android 设备同样可不装任何东西：长按该 Wi-Fi → 修改网络 → 高级 → 代理手动，填同样的地址端口。")
        )
        steps.forEach { step ->
            Text(step, fontSize = 14.sp, modifier = Modifier.padding(vertical = 3.dp))
        }
    }

    @Composable
    fun ClientModeView() {
        Text(L10n.tr("Connect to host", "连接到主机"), modifier = Modifier.fillMaxWidth(), fontSize = 24.sp, fontWeight = FontWeight.Bold)
        Text(L10n.tr("Join this device to the host phone network.", "把本设备接入主机手机的网络。"), modifier = Modifier.fillMaxWidth(), color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(modifier = Modifier.height(20.dp))
        StatusCard(
            title = L10n.tr("Connection channel", "连接通道"),
            isActive = isClientRunning.value,
            activeColor = Color(0xFF2196F3),
            icon = Icons.Default.Security
        )

        Spacer(modifier = Modifier.height(16.dp))

        SetupSteps(listOf(
            L10n.tr("Join the host phone Wi-Fi network in system settings.", "在系统设置中加入主机手机的 Wi-Fi 网络。"),
            L10n.tr("Enter the proxy IP and port shown on the host page.", "填入主机页显示的代理 IP 和端口。"),
            L10n.tr("Start the connection and choose Allow when the system prompt appears.", "启动连接，并在系统弹出提示时选择允许。")
        ))
        Spacer(modifier = Modifier.height(16.dp))

        if (isHostRunning.value) {
            ConflictCard(message = L10n.tr("Hotspot sharing is running; the connection cannot start during sharing.", "热点分享正在运行，分享期间无法启动连接。"))
        }

        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            // FIXED: Uses the theme surface color
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(L10n.tr("Host address", "主机地址"), fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.onSurface)
                Spacer(modifier = Modifier.height(12.dp))

                OutlinedTextField(
                    value = clientIp.value,
                    onValueChange = { clientIp.value = it },
                    label = { Text(L10n.tr("Host IP address", "主机 IP 地址")) },
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = { Icon(Icons.Default.Lan, null) },
                    shape = RoundedCornerShape(12.dp),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    singleLine = true,
                    enabled = !isClientRunning.value && !isHostRunning.value
                )

                Spacer(modifier = Modifier.height(8.dp))

                OutlinedTextField(
                    value = clientPort.value,
                    onValueChange = { clientPort.value = it },
                    label = { Text(L10n.tr("Port", "端口")) },
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = { Icon(Icons.Default.Numbers, null) },
                    shape = RoundedCornerShape(12.dp),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    singleLine = true,
                    enabled = !isClientRunning.value && !isHostRunning.value
                )

                Spacer(modifier = Modifier.height(8.dp))

                // v1.5.43: SOCKS5 credentials (host enforces authentication) - pre-fills the identical
                // initial credentials on both ends; editable when connecting (matches the host settings page).
                OutlinedTextField(
                    value = clientUser.value,
                    onValueChange = { clientUser.value = it },
                    label = { Text(L10n.tr("SOCKS5 username (8-13 alphanumeric)", "SOCKS5 账号（8-13 位字母数字）")) },
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = { Icon(Icons.Default.Person, null) },
                    shape = RoundedCornerShape(12.dp),
                    singleLine = true,
                    enabled = !isClientRunning.value && !isHostRunning.value
                )

                Spacer(modifier = Modifier.height(8.dp))

                var clientPassVisible by remember { mutableStateOf(false) }
                OutlinedTextField(
                    value = clientPass.value,
                    onValueChange = { clientPass.value = it },
                    label = { Text(L10n.tr("SOCKS5 password (8-13 alphanumeric)", "SOCKS5 密码（8-13 位字母数字）")) },
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = { Icon(Icons.Default.Lock, null) },
                    trailingIcon = {
                        IconButton(onClick = { clientPassVisible = !clientPassVisible }) {
                            Icon(
                                imageVector = if (clientPassVisible) Icons.Default.VisibilityOff else Icons.Default.Visibility,
                                contentDescription = if (clientPassVisible) L10n.tr("Hide password", "隐藏密码") else L10n.tr("Show password", "显示密码")
                            )
                        }
                    },
                    shape = RoundedCornerShape(12.dp),
                    visualTransformation = if (clientPassVisible) VisualTransformation.None else PasswordVisualTransformation(),
                    singleLine = true,
                    enabled = !isClientRunning.value && !isHostRunning.value
                )

                Spacer(modifier = Modifier.height(16.dp))

                if (!isClientRunning.value) {
                    Button(
                        onClick = { handleConnectClient() },
                        modifier = Modifier.fillMaxWidth().height(52.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = MaterialTheme.colorScheme.primary,
                            contentColor = MaterialTheme.colorScheme.background
                        ),
                        shape = RoundedCornerShape(16.dp),
                        enabled = !isHostRunning.value
                    ) {
                        Icon(Icons.Default.PlayArrow, null)
                        Spacer(Modifier.width(8.dp))
                        Text(L10n.tr("Start Connection", "启动连接"), fontWeight = FontWeight.ExtraBold, letterSpacing = 1.sp)
                    }
                } else {
                    Button(
                        onClick = { handleStopClient() },
                        modifier = Modifier.fillMaxWidth().height(52.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = Color(0xFFB00020),
                            contentColor = Color.White
                        ),
                        shape = RoundedCornerShape(16.dp)
                    ) {
                        Icon(Icons.Default.Stop, null)
                        Spacer(Modifier.width(8.dp))
                        Text(L10n.tr("Stop Connection", "停止连接"), fontWeight = FontWeight.ExtraBold)
                    }
                }
            }
        }

        if (isClientRunning.value) {
            Spacer(modifier = Modifier.height(8.dp))
            OutlinedButton(
                onClick = { handleReconnectVPN() },
                modifier = Modifier.fillMaxWidth().height(52.dp),
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.primary),
                colors = ButtonDefaults.outlinedButtonColors(
                    contentColor = MaterialTheme.colorScheme.primary
                ),
                shape = RoundedCornerShape(16.dp)
            ) {
                Icon(Icons.Default.Refresh, null)
                Spacer(Modifier.width(8.dp))
                Text(L10n.tr("Reconnect", "重新连接"), fontWeight = FontWeight.Bold)
            }
        }

        Spacer(modifier = Modifier.height(8.dp))
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(
                containerColor = if (isSystemInDarkTheme()) Color(0xFF2B2B1B) else Color(0xFFFFF8E1)
            ),
            border = BorderStroke(1.dp, Color(0xFFFFC107))
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Text(L10n.tr("About game connectivity", "关于游戏连通性"), fontWeight = FontWeight.Bold, fontSize = 14.sp)
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    L10n.tr("This connection carries UDP via SOCKS5 UDP forwarding: browser, WeChat and web pages work normally; whether games can connect depends on whether their UDP traffic follows standard paths. After connecting, tap the button below to test UDP.", "本连接经 SOCKS5 UDP 转发承载 UDP：浏览器、微信、网页正常，游戏能否联机取决于其 UDP 流量是否走标准路径。连接后点击下方按钮即可实测 UDP。"),
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }

        Spacer(modifier = Modifier.height(4.dp))
        TextButton(onClick = { runUdpCheck() }, enabled = isClientRunning.value) {
            Text(
                if (isClientRunning.value) L10n.tr("Test UDP connectivity (via tunnel)", "检测 UDP 连通性（经隧道实测）")
                else L10n.tr("Test UDP connectivity (connect first)", "检测 UDP 连通性（先连接）"),
                fontSize = 13.sp
            )
        }
    }

    /** v1.4.3: UDP live test - with the VPN active, DNS queries go through tun2socks to the host SOCKS5 UDP relay. */
    private fun runUdpCheck() {
        logState.add(L10n.tr("Probing UDP (DNS query ${UdpProbe.DEFAULT_DNS_HOST}:53 via the connection tunnel)...", "UDP 检测中（DNS 查询 ${UdpProbe.DEFAULT_DNS_HOST}:53 经连接隧道）..."))
        thread {
            val error = UdpProbe.dnsQueryViaUdp()
            runOnUiThread {
                logState.add(
                    if (error == null) L10n.tr("UDP reachable: OK (query returned via the tunnel)", "✓ UDP 连通：正常（查询已从隧道返回）")
                    else L10n.tr("UDP unreachable: $error", "✗ UDP 不通：$error")
                )
            }
        }
    }

    private fun handleReconnectVPN() {
        logState.add(L10n.tr("Reconnecting...", "正在重新连接..."))
        val stopIntent = Intent(this, MyVpnServiceTun2Socks::class.java).apply {
            action = MyVpnServiceTun2Socks.ACTION_STOP
        }
        startService(stopIntent)
        thread {
            Thread.sleep(2000)
            runOnUiThread {
                startVpnService(
                    clientIp.value,
                    clientPort.value.toIntOrNull() ?: 8080,
                    clientUser.value,
                    clientPass.value
                )
                logState.add(L10n.tr("Reconnected", "已重新连接"))
            }
        }
    }

    @Composable
    fun ConflictCard(message: String) {
        Card(
            modifier = Modifier.fillMaxWidth().padding(bottom = 16.dp),
            colors = CardDefaults.cardColors(
                containerColor = if (isSystemInDarkTheme()) Color(0xFF3D0000) else Color(0xFFFFEBEE)
            ),
            border = BorderStroke(1.dp, Color.Red)
        ) {
            Row(modifier = Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Warning, contentDescription = null, tint = Color.Red)
                Spacer(modifier = Modifier.width(12.dp))
                Text(message, fontSize = 13.sp, color = Color(0xFFB71C1C), fontWeight = FontWeight.Medium)
            }
        }
    }

    @Composable
    fun SetupSteps(steps: List<String>) {
        Column(modifier = Modifier.fillMaxWidth()) {
            Text(L10n.tr("How to use", "使用步骤"), fontSize = 11.sp, fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.primary, letterSpacing = 1.sp)
            Spacer(Modifier.height(8.dp))
            steps.forEachIndexed { index, step ->
                Row(modifier = Modifier.fillMaxWidth().padding(vertical = 5.dp), verticalAlignment = Alignment.Top) {
                    Text("${index + 1}.", modifier = Modifier.width(26.dp), fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.primary)
                    Text(step, modifier = Modifier.weight(1f), fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }

    @Composable
    fun StatusCard(title: String, isActive: Boolean, activeColor: Color, icon: ImageVector) {
        val infiniteTransition = rememberInfiniteTransition(label = "pulse")
        val alpha by infiniteTransition.animateFloat(
            initialValue = 0.3f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(animation = tween(800, easing = LinearEasing), repeatMode = RepeatMode.Reverse),
            label = "alpha"
        )

        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(24.dp),
            colors = CardDefaults.cardColors(
                // Uses theme surface color
                containerColor = if (isActive) activeColor.copy(alpha = 0.08f) else MaterialTheme.colorScheme.surface
            ),
            border = BorderStroke(
                width = if (isActive) 2.dp else 1.dp,
                color = if (isActive) activeColor else MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f)
            )
        ) {
            Row(modifier = Modifier.padding(20.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(52.dp)
                        .background(if (isActive) activeColor else MaterialTheme.colorScheme.onSurface.copy(alpha = 0.05f), CircleShape),
                    contentAlignment = Alignment.Center
                ) {
                    Icon(
                        imageVector = icon,
                        contentDescription = null,
                        tint = if (isActive) Color.White else Color.Gray,
                        modifier = Modifier.size(28.dp)
                    )
                }

                Spacer(modifier = Modifier.width(16.dp))

                Column {
                    Text(
                        text = title,
                        fontWeight = FontWeight.Bold,
                        fontSize = 18.sp,
                        color = MaterialTheme.colorScheme.onSurface // Adapts to Black/White
                    )
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(modifier = Modifier.size(10.dp).clip(CircleShape).background(if (isActive) activeColor.copy(alpha = alpha) else Color.Gray))
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = if (isActive) L10n.tr("Service running", "服务运行中") else L10n.tr("Not running", "未运行"),
                            color = if (isActive) activeColor else Color.Gray,
                            fontWeight = FontWeight.Bold,
                            fontSize = 12.sp
                        )
                    }
                }
            }
        }
    }

    @Composable
    fun DebugLogSection() {
        var showLogs by remember { mutableStateOf(false) }
        val logBgColor = if (isSystemInDarkTheme()) Color(0xFF1E1E1E) else Color(0xFFE0E0E0)
        val logTextColor = if (isSystemInDarkTheme()) Color(0xFF00E676) else Color(0xFF1B5E20)

        Column(modifier = Modifier.fillMaxWidth()) {
            TextButton(onClick = { showLogs = !showLogs }) {
                Icon(if (showLogs) Icons.Default.KeyboardArrowUp else Icons.Default.KeyboardArrowDown, null)
                Text(if (showLogs) L10n.tr("Hide debug logs", "收起调试日志") else L10n.tr("Show debug logs", "显示调试日志"))
            }
            AnimatedVisibility(visible = showLogs) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(200.dp)
                        .background(logBgColor, RoundedCornerShape(12.dp)) // Adaptive background
                        .padding(12.dp)
                ) {
                    Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                        logState.asReversed().forEach { logLine ->
                            Text(text = "➜ $logLine", fontSize = 11.sp, color = logTextColor) // Adaptive text
                        }
                    }
                }
            }
        }
    }

    @Composable
    fun InfoRow(label: String, value: String) {
        val clipboardManager = LocalClipboardManager.current
        Row(modifier = Modifier.padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(modifier = Modifier.weight(1f)) {
                Text(label.uppercase(), fontSize = 10.sp, color = Color.Gray, fontWeight = FontWeight.Bold)
                Text(
                    value,
                    fontFamily = FontFamily.Monospace,
                    fontWeight = FontWeight.ExtraBold,
                    fontSize = 15.sp,
                    color = MaterialTheme.colorScheme.onSurface // Dynamic color
                )
            }
            IconButton(
                onClick = {
                    clipboardManager.setText(AnnotatedString(value))
                },
                modifier = Modifier
                    .background(MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f), CircleShape) // Subtle contrast
                    .size(36.dp)
            ) {
                Icon(
                    Icons.Default.ContentCopy,
                    contentDescription = "Copy $label",
                    modifier = Modifier.size(16.dp),
                    tint = MaterialTheme.colorScheme.primary
                )
            }
        }
    }

    private fun handleStartHost() {
        if (checkHardwareStatus()) {
            if (!isIgnoringBatteryOptimizations()) {
                requestIgnoreBatteryOptimizations()
                return
            }
            val intent = Intent(this, HostService::class.java).apply {
                putExtra("WIFI_BAND", selectedBand.intValue)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent)
            } else {
                startService(intent)
            }
            isHostRunning.value = true
            logState.add(L10n.tr("Starting sharing service...", "正在启动分享服务..."))
            // beta1.0.53: tell the user which keep-alive channels are armed and
            // warn about the one thing we cannot fix from inside the app — an
            // OEM that put us in the restricted standby bucket.
            logState.add(
                L10n.tr(
                    "Keep-alive armed: 2-min watchdog + JobScheduler + boot restore",
                    "保活已启用：2 分钟看门狗 + JobScheduler + 开机恢复"
                )
            )
            if (isBackgroundRestricted()) {
                logState.add(
                    L10n.tr(
                        "Warning: background activity is restricted for LinkHelm. Open App info -> Battery and choose Unrestricted, or the system may still kill sharing.",
                        "警告：LinkHelm 的后台活动被限制。请在「应用信息 → 电池」中选择「无限制」，否则系统仍可能杀掉分享。"
                    )
                )
            }
            logState.add(
                L10n.tr(
                    "Tip: lock LinkHelm in the recent-apps screen so aggressive battery savers cannot reap it.",
                    "提示：在最近任务列表里锁定 LinkHelm，避免被激进省电策略清理。"
                )
            )
            saveServiceState(hostRunning = true)
        }
    }

    private fun handleStopHost() {
        val intent = Intent(this, HostService::class.java).apply { action = "STOP" }
        startService(intent)
        hostInfoState.value = null
        isHostRunning.value = false
        logState.add(L10n.tr("Sharing service stopped.", "分享服务已停止。"))
        saveServiceState(hostRunning = false)
    }

    private fun handleConnectClient() {
        val address = clientIp.value.trim()
        val port = clientPort.value.toIntOrNull()
        val validAddress = address.split('.').let { octets ->
            octets.size == 4 && octets.all { it.isNotEmpty() && it.length <= 3 && (it.toIntOrNull()?.let { number -> number in 0..255 } == true) }
        }
        if (!validAddress || port == null || port !in 1..65535) {
            Toast.makeText(this, L10n.tr("Enter a valid host IP and port (1-65535).", "请输入有效的主机 IP 和端口（1-65535）。"), Toast.LENGTH_LONG).show()
            return
        }
        // v1.5.43: host enforces SOCKS5 credentials - must be 8-13 alphanumeric characters.
        val socksUser = clientUser.value.trim()
        val socksPass = clientPass.value.trim()
        if (!ProxyServer.SOCKS_CREDENTIAL_REGEX.matches(socksUser) ||
            !ProxyServer.SOCKS_CREDENTIAL_REGEX.matches(socksPass)
        ) {
            Toast.makeText(this, L10n.tr("SOCKS5 username/password must be 8-13 alphanumeric characters.", "SOCKS5 账号/密码需 8-13 位英文字母和数字组合。"), Toast.LENGTH_LONG).show()
            return
        }
        if (checkHardwareStatus()) {
            if (!isIgnoringBatteryOptimizations()) {
                requestIgnoreBatteryOptimizations()
                return
            }
            if (getPrivateDnsMode() == "hostname") {
                Toast.makeText(this, L10n.tr("Please turn off Private DNS first!", "请先关闭「私人 DNS」！"), Toast.LENGTH_LONG).show()
                navigateToPrivateDnsSettings()
                return
            }
            prepareVpn(address, port, socksUser, socksPass)
        }
    }

    private fun handleStopClient() {
        val intent = Intent(this, MyVpnServiceTun2Socks::class.java).apply { action = MyVpnServiceTun2Socks.ACTION_STOP }
        startService(intent)
        isClientRunning.value = false
        logState.add(L10n.tr("Connection stopped.", "连接已停止。"))

        // FIX: Use vpnRunning = false
        saveServiceState(vpnRunning = false)
    }

    private fun startVpnService(ip: String, port: Int, socksUser: String, socksPass: String) {
        val intent = Intent(this, MyVpnServiceTun2Socks::class.java).apply {
            putExtra("PROXY_IP", ip)
            putExtra("PROXY_PORT", port)
            putExtra("PROXY_USER", socksUser)
            putExtra("PROXY_PASS", socksPass)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
        isClientRunning.value = true
        logState.add(L10n.tr("Starting connection...", "连接启动中..."))
        // v1.4.3: auto-runs one UDP live test once the tunnel is ready (result goes to the debug log)
        thread {
            Thread.sleep(6_000)
            if (isClientRunning.value) runUdpCheck()
        }

        // FIX: Use vpnRunning instead of hostRunning
        saveServiceState(vpnRunning = true)
    }

    override fun onConnectionInfoAvailable(info: WifiP2pInfo?) {
        if (info != null && info.groupFormed && info.isGroupOwner) {
            isHostRunning.value = true
        }
    }

    fun updateGroupInfo(group: WifiP2pGroup?) {
        if (group != null && group.isGroupOwner) {
            // Fix (v1.2, P0-A): the proxy IP/port come from the observable EndpointState
            // (auto-refreshing); the SSID/passphrase still come from the P2P group.
            val state = HostService.endpointState
            hostInfoState.value = HostInfo(
                ssid = group.networkName ?: "Unknown",
                pass = group.passphrase ?: "N/A",
                ip = state.ip ?: L10n.tr("Pending", "待分配"),
                port = if (state.port > 0) state.port.toString() else "8080"
            )
            isHostRunning.value = true
        } else if (!isServiceRunning(HostService::class.java)) {
            hostInfoState.value = null
            isHostRunning.value = false
        }
    }

    private fun checkPermissions() {
        val permissions = mutableListOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.CHANGE_WIFI_STATE, Manifest.permission.ACCESS_WIFI_STATE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            permissions.add(Manifest.permission.NEARBY_WIFI_DEVICES)
            permissions.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        ActivityCompat.requestPermissions(this, permissions.toTypedArray(), 101)
    }

    private fun checkHardwareStatus(): Boolean {
        val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as android.net.wifi.WifiManager
        if (!wifiManager.isWifiEnabled) {
            startActivity(Intent(Settings.ACTION_WIFI_SETTINGS))
            return false
        }
        return true
    }

    /**
     * beta1.0.53: "background restricted" = the user (or the OEM) switched the
     * app to the restricted standby bucket in App info -> Battery. Granting
     * "allow background activity" is NOT the same setting, and it is the most
     * common reason sharing still dies on Android 12+ even though every
     * permission looks granted.
     */
    private fun isBackgroundRestricted(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) return false
        val am = getSystemService(android.app.ActivityManager::class.java) ?: return false
        return runCatching { am.isBackgroundRestricted }.getOrDefault(false)
    }

    private fun isIgnoringBatteryOptimizations(): Boolean {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            pm.isIgnoringBatteryOptimizations(packageName)
        } else true
    }

    private fun requestIgnoreBatteryOptimizations() {
        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
            data = Uri.parse("package:$packageName")
        }
        startActivity(intent)
    }

    private fun prepareVpn(ip: String, port: Int, socksUser: String, socksPass: String) {
        val intent = VpnService.prepare(this)
        if (intent != null) {
            startActivityForResult(intent, 102)
        } else {
            startVpnService(ip, port, socksUser, socksPass)
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 102 && resultCode == RESULT_OK) {
            startVpnService(
                clientIp.value,
                clientPort.value.toIntOrNull() ?: 8080,
                clientUser.value.trim(),
                clientPass.value.trim()
            )
        }
    }

    private val logReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            intent?.getStringExtra("message")?.let { logState.add(it) }
        }
    }

    override fun onResume() {
        super.onResume()
        receiver = WiFiDirectBroadcastReceiver(manager, channel, this)
        registerReceiver(receiver, intentFilter)
        ContextCompat.registerReceiver(this, logReceiver, IntentFilter("VPN_LOG"), ContextCompat.RECEIVER_NOT_EXPORTED)

        // SYNC STATES ON RESUME
        syncServiceStates()
    }

    override fun onPause() {
        super.onPause()
        try { unregisterReceiver(receiver) } catch (e: Exception) {}
        try { unregisterReceiver(logReceiver) } catch (e: Exception) {}
    }

    private fun getPrivateDnsMode(): String {
        return try {
            Settings.Global.getString(contentResolver, "private_dns_mode") ?: "off"
        } catch (e: Exception) { "unknown" }
    }

    private fun navigateToPrivateDnsSettings() {
        try {
            startActivity(Intent("android.settings.DNS_SETTINGS"))
        } catch (e: Exception) {
            startActivity(Intent(Settings.ACTION_SETTINGS))
        }
    }

    private fun isServiceRunning(serviceClass: Class<*>): Boolean {
        val manager = getSystemService(Context.ACTIVITY_SERVICE) as android.app.ActivityManager
        @Suppress("DEPRECATION")
        for (service in manager.getRunningServices(Integer.MAX_VALUE)) {
            if (serviceClass.name == service.service.className) {
                return true
            }
        }
        return false
    }

    private fun syncServiceStates() {
        isClientRunning.value = isServiceRunning(MyVpnServiceTun2Socks::class.java)
        isHostRunning.value = isServiceRunning(HostService::class.java)

        val fineLocation = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
        val nearbyDevices = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ContextCompat.checkSelfPermission(this, Manifest.permission.NEARBY_WIFI_DEVICES)
        } else {
            PackageManager.PERMISSION_GRANTED
        }

        if (fineLocation == PackageManager.PERMISSION_GRANTED && nearbyDevices == PackageManager.PERMISSION_GRANTED) {
            try {
                manager.requestGroupInfo(channel) { group ->
                    if (group != null) {
                        updateGroupInfo(group)
                    } else {
                        if (!isServiceRunning(HostService::class.java)) {
                            isHostRunning.value = false
                        }
                    }
                }
                manager.requestConnectionInfo(channel, this)
            } catch (e: SecurityException) {
                logState.add(L10n.tr("Sync failed: permission denied", "同步失败：权限被拒"))
            }
        }
    }

    private fun saveServiceState(hostRunning: Boolean? = null, vpnRunning: Boolean? = null) {
        val prefs = getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
        prefs.edit().apply {
            hostRunning?.let { putBoolean("host_was_running", it) }
            vpnRunning?.let { putBoolean("vpn_was_running", it) }
            apply()
        }
    }
}

/**
 * v1.5.45 (beta1.0.1): file-transfer card — single file <= 30 GB (beta1.0.62,
 * was 1 GB), resumable across a dropped link, over the Wi-Fi Direct link only
 * (never the phone's internet exit), independent of the sharing service.
 * See FileTransfer.kt for the wire protocol.
 */
@Composable
fun FileTransferCard() {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE) }
    var targetIp by remember { mutableStateOf("") }
    var dirSet by remember {
        mutableStateOf(prefs.getString(FileTransferProtocol.PREFS_KEY_DIR, null) != null)
    }
    // beta1.0.93: save-directory probe result (null = OK / not probed yet)
    var dirProbeMsg by remember { mutableStateOf<String?>(null) }
    val state = FileTransferBus.state
    // beta1.0.3: show the ACTUAL local IP (dynamic — the Wi-Fi Direct group
    // re-assigns addresses) and the paired-peer set. A transfer is only
    // possible after mutual IP pairing.
    var localIp by remember { mutableStateOf(FileTransferPairs.localLanIp()) }
    var paired by remember { mutableStateOf(FileTransferPairs.pairedSet()) }
    var pairMsg by remember { mutableStateOf<String?>(null) }
    var pairing by remember { mutableStateOf(false) }
    // beta1.0.4: normalize the typed address (drop an optional :port) so the
    // paired-set lookup matches what pair() actually stored.
    val peerHost = FileTransferProtocol.parsePeerAddress(targetIp.trim()).first
    val isTargetPaired = paired.contains(peerHost)

    val dirLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { uri ->
        if (uri != null) {
            runCatching {
                context.contentResolver.takePersistableUriPermission(
                    uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                )
            }
            // beta1.0.93: PROBE the single-folder grant before trusting it —
            // some OEM pickers hand back a URI the app cannot actually write
            // to; that used to surface only mid-transfer as "FAIL storage".
            // (create+delete a marker doc: fast, same-thread.)
            val probeErr = probeSaveDir(context.contentResolver, uri)
            if (probeErr != null) {
                dirSet = false
                dirProbeMsg = probeErr
                prefs.edit().remove(FileTransferProtocol.PREFS_KEY_DIR).apply()
            } else {
                prefs.edit().putString(FileTransferProtocol.PREFS_KEY_DIR, uri.toString()).apply()
                dirSet = true
                dirProbeMsg = null
                // beta1.0.94: sync the singleton DIRECTLY — the 1.0.93 path
                // went through HostService.restartFileTransferLive(), which is
                // a NO-OP when sharing is off (liveInstance == null), so the
                // receiver only started after an app restart.
                runCatching { FileTransferLive.sync(context) }
            }
        }
    }
    val fileLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri ->
        if (uri != null) {
            val user = prefs.getString("socks_user", ProxyServer.DEFAULT_SOCKS_USER)
                ?: ProxyServer.DEFAULT_SOCKS_USER
            val pass = prefs.getString("socks_pass", ProxyServer.DEFAULT_SOCKS_PASS)
                ?: ProxyServer.DEFAULT_SOCKS_PASS
            FileTransferClient.send(targetIp.trim(), user, pass, context.contentResolver, uri)
        }
    }

    Card(
        modifier = Modifier.fillMaxWidth().padding(16.dp),
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.onSurface.copy(alpha = 0.1f))
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                L10n.tr("File Transfer", "文件传输"),
                fontWeight = FontWeight.Bold,
                color = MaterialTheme.colorScheme.onSurface
            )
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                L10n.tr(
                    "Single file up to 30 GB over the Wi-Fi Direct link only — never the phone's internet exit. Independent of sharing; SHA-256 verified.",
                    "单文件最大 30 GB，仅走 Wi-Fi Direct 本地通道，不经手机外网出口；与网络分享互不影响，传输后校验 SHA-256。"
                ),
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Spacer(modifier = Modifier.height(12.dp))

            OutlinedButton(
                onClick = { dirLauncher.launch(null) },
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(12.dp)
            ) {
                Icon(Icons.Default.Folder, null)
                Spacer(Modifier.width(8.dp))
                Text(
                    if (dirSet) L10n.tr("Save directory: set (tap to change)", "接收保存目录：已设置（点击更改）")
                    else L10n.tr("Set the save directory", "设置接收保存目录")
                )
            }
            // beta1.0.93: live receiver status — makes it visible that the
            // phone is receivable even when sharing is OFF (client mode).
            // beta1.0.95: also surface receiver startup failures (port busy,
            // bad dir) instead of a silent dead receiver.
            FileTransferLive.lastSyncError?.let { err ->
                Text(
                    L10n.tr("Receiver error: $err — re-pick the save directory", "接收器错误：$err——请重新选择保存目录"),
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.error
                )
            }
            Text(
                if (FileTransferLive.isRunning())
                    L10n.tr("Receiver listening on :8765 (works even with sharing off)", "接收器监听中（:8765，分享关闭时也可收）")
                else
                    L10n.tr("Receiver off — set the save directory to enable", "接收器未运行——设置保存目录后自动开启"),
                fontSize = 11.sp,
                color = if (FileTransferLive.isRunning()) Color(0xFF4CAF50)
                        else MaterialTheme.colorScheme.onSurfaceVariant
            )
            if (!dirSet) {
                Text(
                    L10n.tr(
                        "Set the save directory BEFORE pairing — without it incoming files cannot be stored.",
                        "请先设置接收保存目录再配对：未设置时将无法保存收到的文件。"
                    ),
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.error
                )
            }
            // beta1.0.93: directory-grant probe failure — show WHY the pick
            // was rejected so the user can re-pick or grant properly.
            dirProbeMsg?.let { msg ->
                Text(
                    L10n.tr("Save directory rejected: $msg", "保存目录不可用：$msg"),
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.error
                )
            }

            Spacer(modifier = Modifier.height(8.dp))

            // beta1.0.3: local address — the ACTUAL IP obtained on the
            // Wi-Fi Direct link (host is usually 192.168.49.1, clients get
            // their own lease); it changes when the group is rebuilt, so it
            // is re-read on every recomposition of this card.
            // beta1.0.58: a refresh button re-reads it on demand — after the
            // group is rebuilt the old lease is gone and the peer needs the
            // new address (e.g. 192.168.49.4).
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    L10n.tr(
                        "This device address (shown on the other device): ${localIp.ifEmpty { "detecting..." }}",
                        "本机地址（填到另一台设备）：${localIp.ifEmpty { "检测中…" }}"
                    ),
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurface,
                    modifier = Modifier.weight(1f)
                )
                IconButton(onClick = { localIp = FileTransferPairs.localLanIp() }) {
                    Icon(
                        Icons.Default.Refresh,
                        contentDescription = L10n.tr(
                            "Refresh the local address",
                            "刷新本机地址"
                        )
                    )
                }
            }
            Text(
                L10n.tr(
                    "The IP changes when the Wi-Fi Direct group is rebuilt — always follow the currently displayed value.",
                    "IP 随 Wi-Fi Direct 组重建而变化，请以当前显示为准。"
                ),
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )

            Spacer(modifier = Modifier.height(8.dp))

            OutlinedTextField(
                value = targetIp,
                onValueChange = {
                    targetIp = it
                    pairMsg = null
                },
                label = { Text(L10n.tr("Peer address (the actual IP shown on the other device / host)", "对方地址（另一台设备或主机显示的实际 IP）")) },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                shape = RoundedCornerShape(12.dp)
            )

            Spacer(modifier = Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = {
                    val ip = targetIp.trim()
                    if (ip.isEmpty()) {
                        pairMsg = "请先填写对方地址"
                        return@TextButton
                    }
                    // beta1.0.58: the save directory must exist before pairing —
                    // a transfer without it cannot be stored on this device.
                    if (!dirSet) {
                        pairMsg = "请先设置接收保存目录（点击上方按钮），再与对方配对"
                        return@TextButton
                    }
                    pairing = true
                    pairMsg = null
                    val user = prefs.getString("socks_user", ProxyServer.DEFAULT_SOCKS_USER)
                        ?: ProxyServer.DEFAULT_SOCKS_USER
                    val pass = prefs.getString("socks_pass", ProxyServer.DEFAULT_SOCKS_PASS)
                        ?: ProxyServer.DEFAULT_SOCKS_PASS
                    thread(name = "FT-Pair", isDaemon = true) {
                        val err = FileTransferClient.pair(ip, user, pass)
                        // beta1.0.96 DISPLAY FIX: the PC acts as a client only —
                        // it pairs INTO this phone (the incoming PAIR stores the
                        // PC's IP here), so PC→phone transfers already work while
                        // this phone's own reverse pair to the PC's :8765 is
                        // refused (the PC runs no listener). That refusal is NOT
                        // a pairing failure: if the peer is already in our paired
                        // set, mutual pairing is complete — show success.
                        pairMsg = if (err != null && FileTransferPairs.isPaired(peerHost))
                            "已与 $peerHost 完成配对（对方已主动配对本机，可直接传输）"
                        else err ?: "已与 $peerHost 完成配对"
                        pairing = false
                        localIp = FileTransferPairs.localLanIp()
                        paired = FileTransferPairs.pairedSet()
                    }
                }) {
                    Text(
                        if (pairing) L10n.tr("Pairing...", "配对中…")
                        else if (isTargetPaired) L10n.tr("Re-pair", "重新配对")
                        else L10n.tr("Pair with this address", "与该地址配对")
                    )
                }
                if (isTargetPaired) {
                    Text(
                        L10n.tr("Paired", "✓ 已配对"),
                        fontSize = 12.sp,
                        color = Color(0xFF4CAF50)
                    )
                }
            }
            pairMsg?.let { msg ->
                Text(
                    msg,
                    fontSize = 11.sp,
                    color = if (msg.startsWith("已与")) Color(0xFF4CAF50) else MaterialTheme.colorScheme.error
                )
            }

            Spacer(modifier = Modifier.height(8.dp))

            Button(
                onClick = { fileLauncher.launch(arrayOf("*/*")) },
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(12.dp),
                enabled = isTargetPaired && !state.active
            ) {
                Icon(Icons.Default.Send, null)
                Spacer(Modifier.width(8.dp))
                Text(
                    if (isTargetPaired) L10n.tr("Send a file to the peer", "发送文件到对方")
                    else L10n.tr("Pair first to send files", "配对后才能发送文件")
                )
            }

            if (state.active) {
                Spacer(modifier = Modifier.height(12.dp))
                Text(
                    L10n.tr(
                        "${if (state.direction == "send") "Sending" else "Receiving"}: ${state.fileName} (${state.status})",
                        "${if (state.direction == "send") "正在发送" else "正在接收"}：${state.fileName}（${state.status}）"
                    ),
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurface
                )
                Spacer(modifier = Modifier.height(6.dp))
                LinearProgressIndicator(
                    progress = {
                        if (state.totalBytes > 0) {
                            (state.doneBytes.toFloat() / state.totalBytes).coerceIn(0f, 1f)
                        } else 0f
                    },
                    modifier = Modifier.fillMaxWidth()
                )
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    "${state.doneBytes / 1024 / 1024} MB / ${state.totalBytes / 1024 / 1024} MB",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                TextButton(onClick = { FileTransferClient.cancel() }) {
                    Text(L10n.tr("Cancel transfer", "取消传输"))
                }
            } else if (state.error != null) {
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    L10n.tr("Last transfer: ${state.error}", "上次传输：${state.error}"),
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.error
                )
            }
        }
    }
}
