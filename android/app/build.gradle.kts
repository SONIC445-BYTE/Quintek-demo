plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.quintek.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.quintek.app"
        // 26 keeps the launcher icon a pure vector adaptive icon -- below it,
        // Android wants legacy PNG mipmaps at five densities.
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.4.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            // The screens are HTML in assets; there is almost no Kotlin to
            // shrink, and minifying only risks stripping the WebView bridge.
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    // The bundles are already-minified single files. Compressing them again
    // costs build time and blocks the WebView from streaming them straight
    // out of the APK.
    androidResources {
        noCompress += listOf("html")
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.webkit:webkit:1.11.0")
    implementation("androidx.activity:activity-ktx:1.9.0")

    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
}

/**
 * The HTML bundles are build output of `tools_build_standalone.py`, not
 * checked-in source. Fail early and say exactly what to run rather than
 * producing an APK whose screens are blank.
 */
val requiredAssets = listOf("pg-revision.html", "quintek-admin.html")

tasks.register("checkQuintekAssets") {
    val assetDir = file("src/main/assets")
    doLast {
        // `assetDir.resolve(...)` rather than `File(assetDir, ...)`: resolve is
        // a kotlin.io extension and needs no import, whereas java.io.File is
        // not reliably in scope in a Kotlin DSL build script.
        val missing = requiredAssets.filter { !assetDir.resolve(it).exists() }
        if (missing.isNotEmpty()) {
            throw GradleException(
                "Missing web bundle(s) in app/src/main/assets: ${missing.joinToString()}\n" +
                "Build them from the repository root with:\n" +
                "    python3 tools_build_standalone.py\n" +
                "That script inlines React, Babel and the API modules so each screen " +
                "runs offline inside the WebView."
            )
        }
    }
}

tasks.named("preBuild") { dependsOn("checkQuintekAssets") }
