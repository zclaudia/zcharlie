"""DB-backed runtime settings (docs/product/settings-plan.md).

* ``registry`` - the explicit list of runtime keys (``SettingSpec``) and the
  validated ``RuntimeSettings`` model;
* ``crypto`` - Fernet encryption of secret keys (``SETTINGS_ENCRYPTION_KEY``);
* ``store`` - ``SettingsStore``: read / validate-and-write / history / revision;
* ``loader`` - ``load_settings()`` (effective ``Settings``) and the one-time
  ``ensure_imported()`` of env / app.yaml values.
"""
