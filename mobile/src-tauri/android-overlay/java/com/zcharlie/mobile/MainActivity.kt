package com.zcharlie.mobile

import android.os.Bundle
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import androidx.activity.OnBackPressedCallback
import androidx.activity.enableEdgeToEdge
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

class MainActivity : TauriActivity() {
  // BACK 分流逻辑只此一份：研究台页（zcharlie.localhost）BACK = 关弹层 → 研究台内
  // 逐级返回 → 根部退到后台（原生壳会自动跳回研究台，走历史返回会打乒乓）；
  // 非研究台页（配对/设置）交还默认分发链（React 的 onBackButtonPress 优先级链）。
  private fun routeBack(fromKey: Boolean): Boolean {
    val web = findWebView()
    if (web != null && web.url?.startsWith("http://zcharlie.localhost") == true) {
      // 拦截协议文档里的 pushState 条目在原生后退列表中被折叠，goBack() 会直接跳回
      // 上一份文档；改为在页面内分流（overlay:back 契约见 shell.js）。
      web.evaluateJavascript(BACK_BRANCH_JS) { result ->
        if (result?.contains("root") == true) moveTaskToBack(true)
      }
      return true
    }
    if (fromKey) return false
    // dispatcher 路径没有"不消费"的说法：暂时让出回调再重派一次，把 BACK 交还
    // 后注册的 tauri/React 链。
    backCallback.isEnabled = false
    try {
      onBackPressedDispatcher.onBackPressed()
    } finally {
      backCallback.isEnabled = true
    }
    return true
  }

  private lateinit var backCallback: OnBackPressedCallback

  override fun onCreate(savedInstanceState: Bundle?) {
    enableEdgeToEdge()
    super.onCreate(savedInstanceState)
    // BACK 有两条管线，都要接：手势导航（Android 13+ 预测性返回；targetSdk 36 上是
    // 默认且唯一管线）路由给 OnBackPressedDispatcher，不经过 dispatchKeyEvent——
    // 这是真机上 BACK 失效的根因；注入按键/外接键盘走 dispatchKeyEvent。addCallback
    // 后加优先：一并绕开 tauri 残留监听器吞 BACK 的问题。
    backCallback = object : OnBackPressedCallback(true) {
      override fun handleOnBackPressed() {
        routeBack(fromKey = false)
      }
    }
    onBackPressedDispatcher.addCallback(this, backCallback)
    // WebView 的 env(safe-area-inset-*) 在不同系统版本上不可靠；
    // 由原生层把状态栏、刘海、导航栏和键盘的 insets 作为内边距，页面不会再被系统栏盖住。
    val content = findViewById<android.view.View>(android.R.id.content)
    ViewCompat.setOnApplyWindowInsetsListener(content) { view, insets ->
      val bars = insets.getInsets(
        WindowInsetsCompat.Type.systemBars() or
          WindowInsetsCompat.Type.displayCutout() or
          WindowInsetsCompat.Type.ime()
      )
      view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
      WindowInsetsCompat.CONSUMED
    }
  }

  override fun dispatchKeyEvent(event: KeyEvent): Boolean {
    if (event.action == KeyEvent.ACTION_DOWN && event.keyCode == KeyEvent.KEYCODE_BACK) {
      if (routeBack(fromKey = true)) return true
    }
    return super.dispatchKeyEvent(event)
  }

  private fun findWebView(view: View = window.decorView): WebView? {
    if (view is WebView) return view
    if (view is ViewGroup) {
      for (index in 0 until view.childCount) {
        findWebView(view.getChildAt(index))?.let { return it }
      }
    }
    return null
  }

  private companion object {
    // 关弹层 → 非根部文档级返回 → 根部标记（activity 侧 moveTaskToBack）。
    private const val BACK_BRANCH_JS =
      "(function(){if(window.zcOverlay&&window.zcOverlay.isOpen()){window.zcOverlay.close();return 'overlay'}" +
        "if(location.pathname!=='/app'&&history.length>1){history.back();return 'history'}return 'root'})()"
  }
}
