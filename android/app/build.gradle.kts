import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Build-time config from the repo-root .env (dotenv KEY=value). Values are never printed.
fun dotenv(key: String): String {
    val f = rootProject.file("../.env")
    if (!f.exists()) return ""
    return f.readLines()
        .map { it.trim() }
        .firstOrNull { !it.startsWith("#") && it.substringBefore("=").trim().removePrefix("export ").trim() == key }
        ?.substringAfter("=")?.trim()?.removeSurrounding("\"")?.removeSurrounding("'")
        ?: ""
}

fun javaString(s: String) = "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

android {
    namespace = "dev.wearable.morninglog"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.wearable.morninglog"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
        buildConfigField("String", "SELF_REPORT_URL", javaString(dotenv("SELF_REPORT_URL")))
        buildConfigField("String", "SELF_REPORT_TOKEN", javaString(dotenv("SELF_REPORT_TOKEN")))
    }

    buildFeatures { buildConfig = true }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions { jvmTarget.set(JvmTarget.JVM_17) }
}
