; Tauri NSIS 安装钩子
; 保证安装/升级完成后桌面一定存在应用快捷方式：
; 默认模板只在完成页勾选或静默安装时创建，更新安装（/UPDATE）会跳过。

!macro NSIS_HOOK_POSTINSTALL
  ; 尊重 /NS（不创建快捷方式）显式参数
  ${If} $NoShortcutMode <> 1
    CreateShortcut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe" "" "$INSTDIR\${MAINBINARYNAME}.exe" 0
    !insertmacro SetLnkAppUserModelId "$DESKTOP\${PRODUCTNAME}.lnk"
  ${EndIf}
!macroend
