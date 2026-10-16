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
  // 内嵌研究台（zcharlie.localhost）装载后，tauri 的 BACK 分发会被 React 页注册后残留的
  // 监听器记录吞掉（AppPlugin.hasListener 在页面卸载后仍为 true）。在 activity 层直接接管：
  // 研究台是 App 的主界面，BACK 等价于 WebView 后退（关弹层 → 研究台内逐级返回）；
  // 研究台根部 BACK 等价于退到后台——原生壳会自动跳回研究台，走历史返回会打乒乓。
  // 其余页面（配对/设置）走原分发链（React 的 onBackButtonPress 优先级链不变）。
  override fun dispatchKeyEvent(event: KeyEvent): Boolean {
    if (event.action == KeyEvent.ACTION_DOWN && event.keyCode == KeyEvent.KEYCODE_BACK) {
      val web = findWebView()
      if (web != null && web.url?.startsWith("http://zcharlie.localhost") == true) {
        // 拦截协议文档里的 pushState 条目在原生后退列表中被折叠，goBack() 会直接跳回
        // 上一份文档；改为在页面内分流：先关覆盖层（其 close 路径自己消费历史条目），
        // 再文档级 history.back()（研究台内逐级返回）；根部退到后台。
        web.evaluateJavascript(
          "(function(){if(window.zcOverlay&&window.zcOverlay.isOpen()){window.zcOverlay.close();return 'overlay'}" +
            "if(location.pathname!=='/app'&&history.length>1){history.back();return 'history'}return 'root'})()",
        ) { result ->
          if (result?.contains("root") == true) moveTaskToBack(true)
        }
        return true
      }
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
}
