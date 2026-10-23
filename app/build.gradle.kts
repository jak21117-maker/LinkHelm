plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.linkhelm.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.linkhelm.app"
        minSdk = 24
        targetSdk = 35
        versionCode = 45
        versionName = "beta1.0.56"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        ndk {
            // v1.5.42: arm64-v8a only (same approach as the Karing mobile client; ship the mainstream ABI).
            // The original 4 ABIs each shipped a libgojni.so (tun2socks AAR), wasting ~36MB compressed
            // size; all physical phones since 2019 are arm64. Split a separate build for old devices/emulators if needed.
            abiFilters.addAll(listOf("arm64-v8a"))
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    kotlinOptions {
        jvmTarget = "11"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.material)
    implementation(libs.androidx.activity)
    implementation(libs.androidx.constraintlayout)
    testImplementation(libs.junit)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.androidx.espresso.core)


    // Local AAR library - lwIP engine (hev-socks5-tunnel, official 2.19.1 build).
    // beta1.0.5: replaces tun2socks entirely. The JNI class hev.htproxy.TProxyService
    // receives the VpnService TUN fd and runs the lwIP PCB stack (TCP/UDP/DNS in one
    // kernel) — see Myvpnservicetun2socks.kt and DEPENDENCY_LOCK.md.
    implementation(files("libs/hev-socks5-tunnel.aar"))

    val composeBom = platform("androidx.compose:compose-bom:2024.02.00")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.activity:activity-compose:1.8.2")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.7.0")
    implementation("androidx.compose.material:material-icons-extended")
}
