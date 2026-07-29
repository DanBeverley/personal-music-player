import 'package:flutter/material.dart';

import '../../logic/audio_provider.dart';

class PlayerLyricsPanel extends StatelessWidget {
  final PlayerState playerState;
  final TrackLyricsState lyricsState;
  final LyricsMeaningState meaningState;
  final bool isMeaningPanelOpen;
  final int activeIndex;
  final GlobalKey Function(int index) lyricKeyFor;
  final double radiusLarge;
  final Color accentColor;
  final VoidCallback onOpenMeaning;
  final VoidCallback onCloseMeaning;
  final VoidCallback onAskNeatie;

  const PlayerLyricsPanel({
    super.key,
    required this.playerState,
    required this.lyricsState,
    required this.meaningState,
    required this.isMeaningPanelOpen,
    required this.activeIndex,
    required this.lyricKeyFor,
    required this.radiusLarge,
    required this.accentColor,
    required this.onOpenMeaning,
    required this.onCloseMeaning,
    required this.onAskNeatie,
  });

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(18, 94, 18, 28),
        child: Container(
          decoration: BoxDecoration(
            color: const Color(0xFF101216).withValues(alpha: 0.94),
            borderRadius: BorderRadius.circular(radiusLarge),
            border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.34),
                blurRadius: 30,
                offset: const Offset(0, 18),
              ),
            ],
          ),
          child: Stack(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(24, 26, 24, 18),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                Row(
                  children: [
                    Icon(
                      Icons.lyrics_rounded,
                      color: Colors.white.withValues(alpha: 0.72),
                      size: 18,
                    ),
                    const SizedBox(width: 10),
                    Text(
                      'Lyrics',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.92),
                        fontSize: 16,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const Spacer(),
                    TextButton.icon(
                      onPressed: onOpenMeaning,
                      style: TextButton.styleFrom(
                        foregroundColor: Colors.white.withValues(alpha: 0.9),
                        padding: const EdgeInsets.symmetric(
                          horizontal: 10,
                          vertical: 8,
                        ),
                      ),
                      icon: const Icon(Icons.auto_awesome_rounded, size: 16),
                      label: Text(
                        lyricsState.hasLyrics
                            ? 'Behind the Lyrics'
                            : 'Explore meaning',
                        style: const TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    const SizedBox(width: 6),
                    if (lyricsState.source != null &&
                        lyricsState.source!.isNotEmpty)
                      Text(
                        lyricsState.source!,
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.34),
                          fontSize: 11,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 16),
                Text(
                  playerState.currentTrackName,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.95),
                    fontSize: 22,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 6),
                Text(
                  playerState.artist ?? 'Artist Unknown',
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.52),
                    fontSize: 14,
                    fontWeight: FontWeight.w500,
                  ),
                ),
                const SizedBox(height: 24),
                Expanded(
                  child: AnimatedSwitcher(
                    duration: const Duration(milliseconds: 240),
                    switchInCurve: Curves.easeOutCubic,
                    switchOutCurve: Curves.easeInCubic,
                    child: () {
                      if (lyricsState.isLoading) {
                        return Center(
                          key: const ValueKey('lyrics-loading'),
                          child: CircularProgressIndicator(color: accentColor),
                        );
                      }
                      if (!lyricsState.hasLyrics) {
                        return Center(
                          key: const ValueKey('lyrics-empty'),
                          child: Padding(
                            padding: const EdgeInsets.symmetric(horizontal: 16),
                            child: Column(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Icon(
                                  Icons.lyrics_outlined,
                                  color: Colors.white.withValues(alpha: 0.38),
                                  size: 42,
                                ),
                                const SizedBox(height: 18),
                                const Text(
                                  'Lyrics are not available for this track yet.',
                                  textAlign: TextAlign.center,
                                  style: TextStyle(
                                    color: Colors.white70,
                                    fontSize: 18,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                                const SizedBox(height: 10),
                                Text(
                                  lyricsState.error ??
                                      'Swipe right or tap Player to return.',
                                  textAlign: TextAlign.center,
                                  style: const TextStyle(
                                    color: Colors.white54,
                                    fontSize: 13,
                                    height: 1.35,
                                  ),
                                ),
                              ],
                            ),
                          ),
                        );
                      }

                      return ListView.builder(
                        key: ValueKey('lyrics-${lyricsState.videoId}'),
                        physics: const BouncingScrollPhysics(
                          parent: AlwaysScrollableScrollPhysics(),
                        ),
                        padding: const EdgeInsets.only(bottom: 140),
                        itemCount: lyricsState.lines.length,
                        itemBuilder: (context, index) {
                          final line = lyricsState.lines[index];
                          final isActive = index == activeIndex;
                          return Padding(
                            key: lyricKeyFor(index),
                            padding: const EdgeInsets.symmetric(vertical: 8),
                            child: AnimatedContainer(
                              duration: const Duration(milliseconds: 260),
                              curve: Curves.easeOutCubic,
                              transform: Matrix4.translationValues(
                                isActive ? 0.0 : 8.0,
                                0.0,
                                0.0,
                              ),
                              child: AnimatedDefaultTextStyle(
                                duration: const Duration(milliseconds: 260),
                                curve: Curves.easeOutCubic,
                                style: TextStyle(
                                  color: isActive
                                      ? Colors.white
                                      : Colors.white.withValues(alpha: 0.34),
                                  fontSize: isActive ? 30 : 24,
                                  fontWeight: isActive
                                      ? FontWeight.w700
                                      : FontWeight.w500,
                                  height: 1.16,
                                ),
                                child: Text(line.text),
                              ),
                            ),
                          );
                        },
                      );
                    }(),
                  ),
                ),
                  ],
                ),
              ),
              AnimatedPositioned(
                duration: const Duration(milliseconds: 280),
                curve: Curves.easeOutCubic,
                top: 0,
                bottom: 0,
                right: isMeaningPanelOpen ? 0 : -360,
                width: 330,
                child: IgnorePointer(
                  ignoring: !isMeaningPanelOpen,
                  child: _LyricsMeaningDrawer(
                    state: meaningState,
                    accentColor: accentColor,
                    onClose: onCloseMeaning,
                    onRetry: onOpenMeaning,
                    onAskNeatie: onAskNeatie,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _LyricsMeaningDrawer extends StatelessWidget {
  const _LyricsMeaningDrawer({
    required this.state,
    required this.accentColor,
    required this.onClose,
    required this.onRetry,
    required this.onAskNeatie,
  });

  final LyricsMeaningState state;
  final Color accentColor;
  final VoidCallback onClose;
  final VoidCallback onRetry;
  final VoidCallback onAskNeatie;

  @override
  Widget build(BuildContext context) {
    final insight = state.insight;
    return Container(
      decoration: BoxDecoration(
        color: const Color(0xFF15171D).withValues(alpha: 0.98),
        borderRadius: const BorderRadius.horizontal(
          left: Radius.circular(28),
        ),
        border: Border(
          left: BorderSide(color: Colors.white.withValues(alpha: 0.08)),
        ),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.42),
            blurRadius: 34,
            offset: const Offset(-14, 0),
          ),
        ],
      ),
      child: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 18, 18, 18),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(Icons.auto_awesome_rounded, color: accentColor, size: 19),
                  const SizedBox(width: 9),
                  const Expanded(
                    child: Text(
                      'Behind the Lyrics',
                      style: TextStyle(
                        color: Colors.white,
                        fontSize: 17,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                  ),
                  IconButton(
                    onPressed: onClose,
                    icon: const Icon(Icons.close_rounded),
                    color: Colors.white70,
                    tooltip: 'Close',
                  ),
                ],
              ),
              const SizedBox(height: 12),
              Expanded(
                child: AnimatedSwitcher(
                  duration: const Duration(milliseconds: 220),
                  child: () {
                    if (state.isLoading) {
                      return Center(
                        key: const ValueKey('meaning-loading'),
                        child: CircularProgressIndicator(color: accentColor),
                      );
                    }
                    if (state.error != null && insight == null) {
                      return _MeaningEmptyState(
                        key: const ValueKey('meaning-error'),
                        icon: Icons.cloud_off_rounded,
                        title: 'Could not read this one yet.',
                        message: state.error!,
                        actionLabel: 'Try again',
                        onAction: onRetry,
                      );
                    }
                    if (insight == null) {
                      return _MeaningEmptyState(
                        key: const ValueKey('meaning-empty'),
                        icon: Icons.auto_stories_rounded,
                        title: 'Ready when you are.',
                        message:
                            'Neatie can give you a brief read of the song\'s mood, themes, and imagery.',
                        actionLabel: 'Interpret',
                        onAction: onRetry,
                      );
                    }
                    return _MeaningInsightView(
                      key: const ValueKey('meaning-ready'),
                      insight: insight,
                      accentColor: accentColor,
                      onAskNeatie: onAskNeatie,
                    );
                  }(),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _MeaningEmptyState extends StatelessWidget {
  const _MeaningEmptyState({
    super.key,
    required this.icon,
    required this.title,
    required this.message,
    required this.actionLabel,
    required this.onAction,
  });

  final IconData icon;
  final String title;
  final String message;
  final String actionLabel;
  final VoidCallback onAction;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, color: Colors.white38, size: 42),
          const SizedBox(height: 16),
          Text(
            title,
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 18,
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            message,
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: Colors.white54,
              fontSize: 13,
              height: 1.35,
            ),
          ),
          const SizedBox(height: 18),
          FilledButton(
            onPressed: onAction,
            child: Text(actionLabel),
          ),
        ],
      ),
    );
  }
}

class _MeaningInsightView extends StatelessWidget {
  const _MeaningInsightView({
    super.key,
    required this.insight,
    required this.accentColor,
    required this.onAskNeatie,
  });

  final LyricsMeaningInsight insight;
  final Color accentColor;
  final VoidCallback onAskNeatie;

  @override
  Widget build(BuildContext context) {
    return ListView(
      physics: const BouncingScrollPhysics(),
      padding: const EdgeInsets.only(bottom: 18),
      children: [
        _MeaningSection(title: 'Quick read', body: insight.summary),
        if (insight.themes.isNotEmpty)
          _MeaningChipSection(title: 'Themes', values: insight.themes),
        if (insight.emotionalTone.trim().isNotEmpty)
          _MeaningSection(title: 'Emotional tone', body: insight.emotionalTone),
        if (insight.contextNotes.isNotEmpty)
          _MeaningListSection(title: 'Context notes', values: insight.contextNotes),
        if (insight.notableImagery.isNotEmpty)
          _MeaningListSection(
            title: 'Imagery',
            values: insight.notableImagery,
          ),
        if (insight.sourceNotes.trim().isNotEmpty)
          _MeaningSection(title: 'Source note', body: insight.sourceNotes),
        const SizedBox(height: 16),
        FilledButton.icon(
          onPressed: onAskNeatie,
          icon: const Icon(Icons.chat_bubble_outline_rounded),
          label: const Text('Ask Neatie about this song'),
        ),
        const SizedBox(height: 10),
        Text(
          'Confidence ${(insight.confidence * 100).round()}%'
          '${insight.cached ? ' / cached' : ''}',
          style: TextStyle(
            color: Colors.white.withValues(alpha: 0.38),
            fontSize: 11,
            fontWeight: FontWeight.w700,
          ),
        ),
      ],
    );
  }
}

class _MeaningSection extends StatelessWidget {
  const _MeaningSection({required this.title, required this.body});

  final String title;
  final String body;

  @override
  Widget build(BuildContext context) {
    if (body.trim().isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _MeaningTitle(title),
          const SizedBox(height: 7),
          Text(
            body,
            style: const TextStyle(
              color: Colors.white70,
              fontSize: 14,
              height: 1.42,
            ),
          ),
        ],
      ),
    );
  }
}

class _MeaningChipSection extends StatelessWidget {
  const _MeaningChipSection({required this.title, required this.values});

  final String title;
  final List<String> values;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _MeaningTitle(title),
          const SizedBox(height: 9),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: values
                .map(
                  (value) => Chip(
                    label: Text(value),
                    backgroundColor: Colors.white.withValues(alpha: 0.08),
                    labelStyle: const TextStyle(
                      color: Colors.white70,
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                    ),
                    side: BorderSide(color: Colors.white.withValues(alpha: 0.08)),
                  ),
                )
                .toList(growable: false),
          ),
        ],
      ),
    );
  }
}

class _MeaningListSection extends StatelessWidget {
  const _MeaningListSection({required this.title, required this.values});

  final String title;
  final List<String> values;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _MeaningTitle(title),
          const SizedBox(height: 7),
          ...values.map(
            (value) => Padding(
              padding: const EdgeInsets.only(bottom: 7),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('- ', style: TextStyle(color: Colors.white54)),
                  Expanded(
                    child: Text(
                      value,
                      style: const TextStyle(
                        color: Colors.white70,
                        fontSize: 13,
                        height: 1.35,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _MeaningTitle extends StatelessWidget {
  const _MeaningTitle(this.title);

  final String title;

  @override
  Widget build(BuildContext context) {
    return Text(
      title.toUpperCase(),
      style: TextStyle(
        color: Colors.white.withValues(alpha: 0.46),
        fontSize: 11,
        fontWeight: FontWeight.w900,
        letterSpacing: 1.2,
      ),
    );
  }
}
