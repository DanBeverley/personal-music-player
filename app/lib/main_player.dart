import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'logic/audio_provider.dart';
import 'logic/audio_provider_queue.dart';
import 'logic/details_provider.dart';
import 'ui/app_theme_tokens.dart';
import 'ui/neatie_components.dart';
import 'widgets/player/player_lyrics_panel.dart';
import 'widgets/player/player_now_playing_panel.dart';
import 'widgets/player/player_queue_widgets.dart';
import 'widgets/player/player_queue_sheet.dart';

const _accentGrey = neatieActive;
const _surfaceGreyAlt = neatieRaised;
const double _radiusLarge = neatieRadiusLarge;
const double _radiusMedium = neatieRadiusMedium;

class FullPlayerScreen extends ConsumerStatefulWidget {
  const FullPlayerScreen({super.key});

  @override
  ConsumerState<FullPlayerScreen> createState() => _FullPlayerScreenState();
}

class _FullPlayerScreenState extends ConsumerState<FullPlayerScreen> {
  static const double _queueInitialSize = 0.72;
  static const double _queueMinSize = 0.44;
  static const double _queueExpandedSize = 0.92;

  double? _dragValue;
  late final PageController _pageController;
  late final DraggableScrollableController _queueSheetController;
  late final TextEditingController _queueSearchController;
  final Map<int, GlobalKey> _lyricsLineKeys = <int, GlobalKey>{};
  final Set<String> _removingQueueTrackIds = <String>{};
  Timer? _queueSearchDebounce;
  List<Map<String, dynamic>> _queueSearchResults = const [];
  bool _isQueueSearchLoading = false;
  bool _isQueueSheetOpen = false;
  int _queueSearchRequestVersion = 0;
  String? _lastLyricsVideoId;
  StateSetter? _queueSheetStateSetter;
  int _activePage = 0;
  int _lastSyncedLyricIndex = -1;

  @override
  void initState() {
    super.initState();
    _pageController = PageController(initialPage: 0);
    _queueSheetController = DraggableScrollableController();
    _queueSearchController = TextEditingController()
      ..addListener(_onQueueSearchChanged);
  }

  @override
  void dispose() {
    _queueSearchDebounce?.cancel();
    _queueSearchController
      ..removeListener(_onQueueSearchChanged)
      ..dispose();
    _pageController.dispose();
    _queueSheetController.dispose();
    super.dispose();
  }

  void _openLyricsPanel() {
    if (!_pageController.hasClients) return;
    _lastSyncedLyricIndex = -1;
    _dismissQueueSheetIfOpen();
    _pageController.nextPage(
      duration: const Duration(milliseconds: 340),
      curve: Curves.easeOutCubic,
    );
  }

  void _closeLyricsPanel() {
    if (!_pageController.hasClients) return;
    _pageController.previousPage(
      duration: const Duration(milliseconds: 320),
      curve: Curves.easeOutCubic,
    );
  }

  void _dismissQueueSheetIfOpen() {
    if (!_isQueueSheetOpen) return;
    Navigator.of(context).maybePop();
  }

  void _refreshQueueSheet() {
    _queueSheetStateSetter?.call(() {});
  }

  void _openQueueSheet() {
    if (_isQueueSheetOpen || !mounted) return;
    _clearQueueSearch();
    setState(() => _isQueueSheetOpen = true);
    unawaited(
      showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        backgroundColor: Colors.transparent,
        barrierColor: Colors.black.withValues(alpha: 0.28),
        builder: (sheetContext) {
          return StatefulBuilder(
            builder: (context, setSheetState) {
              _queueSheetStateSetter = setSheetState;
              return Consumer(
                builder: (context, ref, _) {
                  final queueState = ref.watch(playbackQueueProvider);
                  final queueNotifier =
                      ref.read(playbackQueueProvider.notifier);
                  return DraggableScrollableSheet(
                    controller: _queueSheetController,
                    initialChildSize: _queueInitialSize,
                    minChildSize: _queueMinSize,
                    maxChildSize: _queueExpandedSize,
                    expand: false,
                    snap: true,
                    snapSizes: const [_queueMinSize, _queueExpandedSize],
                    builder: (context, scrollController) => _buildQueueSheet(
                      context,
                      queueState,
                      queueNotifier,
                      scrollController,
                      onDismiss: () => Navigator.of(sheetContext).pop(),
                    ),
                  );
                },
              );
            },
          );
        },
      ).whenComplete(() {
        _clearQueueSearch();
        _queueSheetStateSetter = null;
        if (mounted) {
          setState(() => _isQueueSheetOpen = false);
        }
      }),
    );
  }

  void _onQueueSearchChanged() {
    final query = _queueSearchController.text.trim();
    _queueSearchDebounce?.cancel();
    if (query.isEmpty) {
      if (_queueSearchResults.isNotEmpty || _isQueueSearchLoading) {
        setState(() {
          _queueSearchResults = const [];
          _isQueueSearchLoading = false;
        });
        _refreshQueueSheet();
      }
      return;
    }

    _queueSearchDebounce = Timer(const Duration(milliseconds: 260), () {
      unawaited(_searchQueueTracks(query));
    });
  }

  Future<void> _searchQueueTracks(String query) async {
    final requestVersion = ++_queueSearchRequestVersion;
    if (mounted) {
      setState(() => _isQueueSearchLoading = true);
      _refreshQueueSheet();
    }
    try {
      final fetchResult = await fetchSearchPayload(
        ref.read,
        query,
        limit: 8,
        timeout: const Duration(seconds: 10),
        deferSideSurfaces: true,
      );
      if (!mounted || requestVersion != _queueSearchRequestVersion) return;

      if (fetchResult.hasPayload) {
        final payload = fetchResult.payload!;
        final results = (payload['results'] as List<dynamic>? ?? const []);
        setState(() {
          _queueSearchResults = results
              .map((entry) => Map<String, dynamic>.from(entry as Map))
              .toList(growable: false);
        });
        _refreshQueueSheet();
      } else {
        setState(() => _queueSearchResults = const []);
        _refreshQueueSheet();
      }
    } catch (_) {
      if (!mounted || requestVersion != _queueSearchRequestVersion) return;
      setState(() => _queueSearchResults = const []);
      _refreshQueueSheet();
    } finally {
      if (mounted && requestVersion == _queueSearchRequestVersion) {
        setState(() => _isQueueSearchLoading = false);
        _refreshQueueSheet();
      }
    }
  }

  void _clearQueueSearch() {
    _queueSearchDebounce?.cancel();
    _queueSearchRequestVersion++;
    if (_queueSearchController.text.isNotEmpty) {
      _queueSearchController.clear();
    }
    if (_queueSearchResults.isNotEmpty || _isQueueSearchLoading) {
      setState(() {
        _queueSearchResults = const [];
        _isQueueSearchLoading = false;
      });
      _refreshQueueSheet();
    }
  }

  String _sleepTimerBadgeLabel(int remainingSeconds) {
    if (remainingSeconds <= 0) return 'Off';
    final hours = remainingSeconds ~/ 3600;
    final minutes = (remainingSeconds % 3600) ~/ 60;
    if (hours > 0) {
      return minutes > 0 ? '${hours}h ${minutes}m' : '${hours}h';
    }
    return '${minutes}m';
  }

  Future<void> _animateQueueRemoval(
    String trackId,
    VoidCallback onRemove,
  ) async {
    if (_removingQueueTrackIds.contains(trackId)) return;
    setState(() => _removingQueueTrackIds.add(trackId));
    _refreshQueueSheet();
    await Future.delayed(const Duration(milliseconds: 240));
    if (!mounted) return;
    onRemove();
    setState(() => _removingQueueTrackIds.remove(trackId));
    _refreshQueueSheet();
  }

  void _ensureLyricsLoaded(String? videoId) {
    if (_lastLyricsVideoId == videoId) return;
    _lastLyricsVideoId = videoId;
    _lyricsLineKeys.clear();
    _lastSyncedLyricIndex = -1;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      if (videoId == null || videoId.isEmpty) {
        ref.read(lyricsProvider.notifier).clear();
        return;
      }
      final playerState = ref.read(audioPlayerProvider);
      unawaited(
        ref.read(lyricsProvider.notifier).fetchLyrics(
              videoId,
              title: playerState.currentTrackName,
              artist: playerState.artist,
            ),
      );
    });
  }

  int _activeLyricsIndex(
    TrackLyricsState lyricsState,
    PlayerState playerState,
  ) {
    if (!lyricsState.hasLyrics || lyricsState.lines.isEmpty) return -1;

    final currentMs = playerState.currentPositionMs;
    if (lyricsState.hasTimestamps) {
      for (var i = 0; i < lyricsState.lines.length; i++) {
        final line = lyricsState.lines[i];
        final start = line.startTimeMs ?? 0;
        final end = line.endTimeMs ?? (start + 2500);
        if (currentMs >= start && currentMs < end) {
          return i;
        }
      }
      for (var i = lyricsState.lines.length - 1; i >= 0; i--) {
        final line = lyricsState.lines[i];
        if (line.startTimeMs != null && currentMs >= line.startTimeMs!) {
          return i;
        }
      }
      return -1;
    }

    if (playerState.duration <= 0) return 0;
    final progress =
        (playerState.currentPosition / playerState.duration).clamp(0.0, 0.999);
    return (progress * lyricsState.lines.length)
        .floor()
        .clamp(0, lyricsState.lines.length - 1);
  }

  GlobalKey _lyricKeyFor(int index) =>
      _lyricsLineKeys.putIfAbsent(index, GlobalKey.new);

  void _syncLyricsScroll(int activeIndex) {
    if (_activePage != 1 || activeIndex < 0) return;
    if (_lastSyncedLyricIndex == activeIndex) return;
    _lastSyncedLyricIndex = activeIndex;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final targetContext = _lyricKeyFor(activeIndex).currentContext;
      if (targetContext == null) return;
      final renderObject = targetContext.findRenderObject();
      if (renderObject is! RenderObject || !renderObject.attached) return;
      if (Scrollable.maybeOf(targetContext) == null) return;
      try {
        Scrollable.ensureVisible(
          targetContext,
          duration: const Duration(milliseconds: 420),
          curve: Curves.easeOutCubic,
          alignment: 0.28,
        );
      } catch (_) {
        // The line may have been detached by a fast page transition.
      }
    });
  }

  Widget _buildQueueSheet(BuildContext context, PlaybackQueueState queueState,
      PlaybackQueueNotifier queueNotifier, ScrollController scrollController,
      {required VoidCallback onDismiss}) {
    return PlayerQueueSheet(
      queueState: queueState,
      queueNotifier: queueNotifier,
      scrollController: scrollController,
      onDismiss: onDismiss,
      searchController: _queueSearchController,
      isQueueSearchLoading: _isQueueSearchLoading,
      queueSearchResults: _queueSearchResults,
      onClearSearch: _clearQueueSearch,
      removingQueueTrackIds: _removingQueueTrackIds,
      onAnimateQueueRemoval: _animateQueueRemoval,
      radiusLarge: _radiusLarge,
      radiusMedium: _radiusMedium,
      accentColor: _accentGrey,
    );
  }

  Widget _buildLyricsPanel(
    BuildContext context,
    PlayerState playerState,
    TrackLyricsState lyricsState,
  ) {
    final activeIndex = _activeLyricsIndex(lyricsState, playerState);
    _syncLyricsScroll(activeIndex);
    return PlayerLyricsPanel(
      playerState: playerState,
      lyricsState: lyricsState,
      activeIndex: activeIndex,
      lyricKeyFor: _lyricKeyFor,
      radiusLarge: _radiusLarge,
      accentColor: _accentGrey,
    );
  }

  Widget _buildPlayerPanel(
    BuildContext context,
    PlayerState playerState,
    AudioPlayerNotifier audioNotifier,
    PlaybackQueueState queueState,
  ) {
    return PlayerNowPlayingPanel(
      playerState: playerState,
      audioNotifier: audioNotifier,
      queueState: queueState,
      dragValue: _dragValue,
      onSeekDragChanged: (value) => setState(() => _dragValue = value),
      onSeekDragEnd: (value) {
        unawaited(audioNotifier.seek((value * playerState.duration).toInt()));
        setState(() => _dragValue = null);
      },
      onOpenQueueSheet: _openQueueSheet,
      onDismissPlayer: () {
        if (Navigator.canPop(context)) {
          Navigator.pop(context);
        }
      },
      accentColor: _accentGrey,
      surfaceColor: _surfaceGreyAlt,
      radiusLarge: _radiusLarge,
      radiusMedium: _radiusMedium,
      sleepTimerBadgeLabel: _sleepTimerBadgeLabel,
    );
  }

  @override
  Widget build(BuildContext context) {
    final playerState = ref.watch(audioPlayerProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final lyricsState = ref.watch(lyricsProvider);
    final queueState = ref.watch(playbackQueueProvider);
    _ensureLyricsLoaded(playerState.videoId);

    return Scaffold(
      extendBodyBehindAppBar: true,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.keyboard_arrow_down, color: Colors.white),
          onPressed: () => Navigator.pop(context),
        ),
        title: const SizedBox.shrink(),
        centerTitle: true,
        actions: [
          IconButton(
            icon: const Icon(Icons.queue_music_rounded, color: Colors.white),
            onPressed: _openQueueSheet,
          ),
        ],
      ),
      body: Stack(
        fit: StackFit.expand,
        children: [
          NeatieBackground(
            child: PageView(
              controller: _pageController,
              onPageChanged: (index) {
                setState(() => _activePage = index);
                if (index == 1) {
                  _lastSyncedLyricIndex = -1;
                  _dismissQueueSheetIfOpen();
                }
              },
              children: [
                _buildPlayerPanel(
                  context,
                  playerState,
                  audioNotifier,
                  queueState,
                ),
                _buildLyricsPanel(context, playerState, lyricsState),
              ],
            ),
          ),
          Positioned(
            right: _activePage == 0 ? 0 : null,
            left: _activePage == 1 ? 0 : null,
            top: MediaQuery.of(context).size.height * 0.22,
            child: PlayerPanelTab(
              icon: _activePage == 0
                  ? Icons.lyrics_rounded
                  : Icons.graphic_eq_rounded,
              pointLeft: _activePage == 0,
              onTap: _activePage == 0 ? _openLyricsPanel : _closeLyricsPanel,
              surfaceColor: _surfaceGreyAlt,
            ),
          ),
        ],
      ),
    );
  }
}
