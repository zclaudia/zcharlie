# zcharlie Android 客户端

## 构建 Debug APK

需要 Node.js 22、pnpm、Rust、Tauri CLI、JDK 17、Android SDK/NDK，以及
`aarch64-linux-android` Rust target。`scripts/env.sh` 会优先使用项目本机的 JDK/SDK
路径；其他环境请设置 `JAVA_HOME`、`ANDROID_HOME` 和 `NDK_HOME`。

在仓库根目录执行以下命令即可安装锁定依赖并构建 Debug APK：

```bash
cd mobile && pnpm install --frozen-lockfile && pnpm tauri:android:debug
```

产物位于：

```text
mobile/src-tauri/gen/android/app/build/outputs/apk/universal/debug/app-universal-debug.apk
```

首次构建会由 `scripts/tauri.sh` 初始化 `src-tauri/gen/android`，该目录是生成内容，
不会提交。需要本机明文网关联调时，包装脚本只向 Debug manifest 注入
`usesCleartextTraffic`；Release manifest 不含该设置，正式构建需使用 HTTPS 网关。

## 本地开发

```bash
cd mobile
pnpm install --frozen-lockfile
pnpm test
pnpm tauri:android:dev
```

Tauri Rust 版本、插件版本和前端依赖以 `src-tauri/Cargo.lock` 与
`pnpm-lock.yaml` 为准。设备凭证由 Store 插件写入应用私有数据目录；不要把配对信息、
本机凭证或签名密钥提交到仓库。
