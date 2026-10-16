package com.zcharlie.mobile

import android.os.Bundle
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import androidx.activity.enableEdgeToEdge
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

class MainActivity : TauriActivity() {
  // BACK 分流逻辑只此一份：研究台页（zcharlie.localhost）BACK = 关弹层 → 研究台内
  // 逐级返回 → 根部退到后台（原生壳会自动跳回研究台，走历史返回会打乒乓）；
  // 非研究台页（配对/设置）交还默认分发链（React 的 onBackButtonPress 优先级链）。
  private fun routeBack(): Boolean {
    val web = findWebView()
    if (web != null && web.url?.startsWith("http://zcharlie.localhost") == true) {
      // 拦截协议文档里的 pushState 条目在原生后退列表中被折叠，goBack() 会直接跳回
      // 上一份文档；改为在页面内分流（overlay:back 契约见 shell.js）。手势导航走
      // dispatcher 管线（AppPlugin 回调 + shell.js 的 back-button 监听），不经过这里。
      web.evaluateJavascript(BACK_BRANCH_JS) { result ->
        if (result?.contains("root") == true) moveTaskToBack(true)
      }
      return true
    }
    return false
  }

  override fun onCreate(savedInstanceState: Bundle?) {
    enableEdgeToEdge()
    super.onCreate(savedInstanceState)
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
      if (routeBack()) return true
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
