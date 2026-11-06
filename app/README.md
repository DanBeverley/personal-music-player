# Neatie Flutter clients

Main worktree owns Android. The separate web worktree conditionally imports main_android.dart or web/web_app.dart and owns the deployed browser implementation. Shared backend contracts do not imply identical UI, audio or worktree contents.

Read the [project map](../docs/handoff/PROJECT_OVERVIEW.md), [current status](../docs/handoff/CURRENT_STATUS.md), [full queue](../docs/handoff/NEXT_STEPS.md) and [architecture](../docs/handoff/ARCHITECTURE.md) before changing code.

Android release native libraries are built through CMake/managedJniLibs. Local .env, runtime/cookies and legacy native archives are outside Git; preserve needed private files separately. User runs flutter analyze; focused test/build commands are in NEXT_STEPS. See [release and self-update](../docs/ANDROID_RELEASE.md) and [authentication](../supabase/README.md).
