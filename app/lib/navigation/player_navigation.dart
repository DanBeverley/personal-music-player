import 'package:flutter/material.dart';

import '../main_player.dart';

const String fullPlayerRouteName = '/neatie/player';

bool _isOpeningFullPlayer = false;

Future<void> openFullPlayer(BuildContext context) async {
  if (_isOpeningFullPlayer) return;
  final currentRoute = ModalRoute.of(context);
  if (currentRoute?.settings.name == fullPlayerRouteName) return;

  _isOpeningFullPlayer = true;
  try {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        settings: const RouteSettings(name: fullPlayerRouteName),
        builder: (_) => const FullPlayerScreen(),
      ),
    );
  } finally {
    _isOpeningFullPlayer = false;
  }
}
