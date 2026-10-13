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

## 系统栏与安全区

应用为 edge-to-edge 模式。`src-tauri/android-overlay/` 中的 `MainActivity.kt` 把状态栏、
刘海、导航栏和键盘的 insets 作为原生内边距，WebView 内容不会被系统标题栏遮挡；
`themes.xml` 把窗口底色设为页面背景色。`gen/android` 是生成目录，包装脚本在每次
build/dev 时把 overlay 复制进去。

## 应用图标

Android 图标沿用 `docs/brand/zcharlie-icon-v2.png` 的蓝底白色 C 形框与三根 K 线，
logo 相比初版图标缩小 15%，保持居中；普通图标的矢量源为
`src-tauri/icons/sources/app-icon.svg`。
`src-tauri/icons/android/` 保存 mdpi 至 xxxhdpi 的普通、圆形、自适应和单色图标；
自适应前景的矢量版本位于 `src-tauri/icons/sources/android-foreground.svg`，
已留出安全边距，背景使用品牌蓝 `#2357D9`。

更新品牌素材后，在 `mobile/` 执行 `pnpm icons` 重新生成。Android 构建和开发命令
会自动将这些资源复制到生成工程，因此删除 `src-tauri/gen/android` 后重建也能保留图标。

Tauri Rust 版本、插件版本和前端依赖以 `src-tauri/Cargo.lock` 与
`pnpm-lock.yaml` 为准。设备凭证由 Store 插件写入应用私有数据目录；不要把配对信息、
本机凭证或签名密钥提交到仓库。
