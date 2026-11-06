import 'package:flutter/material.dart';

import '../main_player.dart';

const String fullPlayerRouteName = '/neatie/player';

bool _isOpeningFullPlayer = false;

Future<void> openFullPlayer(BuildContext context) async {
  if (_isOpeningFullPlayer) return;
  final currentRoute = ModalRoute.of(context);
  if (currentRoute?.settings.name == fullPlayerRouteName) return;

  await openFullPlayerWithNavigator(Navigator.of(context));
}

Future<void> openFullPlayerWithNavigator(NavigatorState? navigator) async {
  if (_isOpeningFullPlayer || navigator == null) return;

  _isOpeningFullPlayer = true;
  try {
    await navigator.push(
      MaterialPageRoute<void>(
        settings: const RouteSettings(name: fullPlayerRouteName),
        builder: (_) => const FullPlayerScreen(),
      ),
    );
  } finally {
    _isOpeningFullPlayer = false;
  }
}
