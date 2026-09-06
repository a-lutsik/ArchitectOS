plugins {
    java
    kotlin("jvm") version "2.0.21"
    id("org.jetbrains.intellij.platform") version "2.2.1"
}

group = "com.architectos"
version = "0.2.1"

repositories {
    mavenCentral()
    intellijPlatform { defaultRepositories() }
}

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3") {
        // Platform already ships Kotlin stdlib + annotations; bundling 2.0.20
        // fights IntelliJ 2025–2026 (Kotlin 2.1/2.2).
        exclude(group = "org.jetbrains.kotlin")
        exclude(group = "org.jetbrains", module = "annotations")
    }
    intellijPlatform {
        create("IC", "2024.3.6")
        bundledPlugin("com.intellij.java")
        instrumentationTools()
        pluginVerifier()
        zipSigner()
    }
}

intellijPlatform {
    pluginConfiguration {
        id = "com.architectos.memory"
        name = "ArchitectOS Memory"
        version = project.version.toString()
        description = "Search, view and grow your ArchitectOS team memory from the IDE."
        ideaVersion {
            sinceBuild = "243"
            // Compiled against 2024.3; the Gradle plugin would otherwise stamp
            // until-build="243.*" and reject IDEA / PyCharm 2025–2026 (251–262).
            untilBuild = provider { null }
        }
    }
}

tasks {
    compileKotlin { kotlinOptions.jvmTarget = "17" }
    compileTestKotlin { kotlinOptions.jvmTarget = "17" }
}
