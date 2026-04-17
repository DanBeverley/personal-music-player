part of 'main.dart';

class MiniPlayer extends ConsumerWidget {
  const MiniPlayer({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final playerState = ref.watch(audioPlayerProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final videoId = playerState.videoId;

    final progress = playerState.duration > 0
        ? (playerState.currentPosition / playerState.duration).clamp(0.0, 1.0)
        : 0.0;
    void openPlayer() {
      Navigator.of(context)
          .push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
    }

    return GestureDetector(
      onTap: openPlayer,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 18, sigmaY: 18),
          child: Container(
            height: 68,
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(12),
              color: const Color(0xFF1B1D20).withValues(alpha: 0.96),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
                width: 1,
              ),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.34),
                  blurRadius: 28,
                  offset: const Offset(0, 14),
                ),
              ],
            ),
            child: Row(
              children: [
                GestureDetector(
                  onTap: () {
                    unawaited(
                      playerState.isPlaying
                          ? audioNotifier.pause()
                          : audioNotifier.play(),
                    );
                  },
                  child: Container(
                    width: 42,
                    height: 42,
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(10),
                      border: Border.all(
                        color: const Color(0xFF767D85).withValues(alpha: 0.45),
                        width: 1.4,
                      ),
                    ),
                    child: Stack(
                      alignment: Alignment.center,
                      children: [
                        ClipRRect(
                          borderRadius: BorderRadius.circular(8),
                          child: AppArtwork(
                            thumbnail: playerState.thumbnail,
                            videoId: videoId,
                            width: 36,
                            height: 36,
                            radius: 8,
                            heroTag: 'album_art_${playerState.currentTrackName}',
                          ),
                        ),
                        Container(
                          width: 36,
                          height: 36,
                          decoration: BoxDecoration(
                            color: Colors.black.withValues(alpha: 0.46),
                            borderRadius: BorderRadius.circular(8),
                          ),
                        ),
                        playerState.isDownloading
                            ? const SizedBox(
                                width: 22,
                                height: 22,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2.4,
                                  color: Colors.white,
                                ),
                              )
                            : Icon(
                                playerState.isPlaying
                                    ? Icons.pause_rounded
                                    : Icons.play_arrow_rounded,
                                color: Colors.white,
                                size: 18,
                              ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Text(
                        playerState.currentTrackName,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Colors.white,
                          fontWeight: FontWeight.w700,
                          fontSize: 13,
                          letterSpacing: -0.2,
                        ),
                      ),
                      const SizedBox(height: 1),
                      Text(
                        playerState.artist ?? 'Unknown Artist',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.64),
                          fontSize: 10.5,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                      const SizedBox(height: 5),
                      ClipRRect(
                        borderRadius: BorderRadius.circular(999),
                        child: LinearProgressIndicator(
                          value: progress,
                          backgroundColor: Colors.white.withValues(alpha: 0.08),
                          color: const Color(0xFF7A8088),
                          minHeight: 2.5,
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 4),
                _MiniPlayerAction(
                  icon: Icons.queue_music_rounded,
                  onTap: openPlayer,
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _MiniPlayerAction extends StatelessWidget {
  final IconData icon;
  final VoidCallback onTap;

  const _MiniPlayerAction({
    required this.icon,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: Container(
        width: 28,
        height: 28,
        decoration: BoxDecoration(
          color: const Color(0xFF2A2D31),
          borderRadius: BorderRadius.circular(6),
        ),
        child: Icon(
          icon,
          color: Colors.white.withValues(alpha: 0.72),
          size: 15,
        ),
      ),
    );
  }
}

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
                  final queueNotifier = ref.read(playbackQueueProvider.notifier);
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
      final res = await appHttpClient
          .post(
            buildProxyUri('/search'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({"query": query, "limit": 8}),
          )
          .timeout(const Duration(seconds: 10));
      if (!mounted || requestVersion != _queueSearchRequestVersion) return;

      if (res.statusCode == 200) {
        final payload = jsonDecode(res.body) as Map<String, dynamic>;
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

  String _formatSleepTimerLabel(int remainingSeconds) {
    if (remainingSeconds <= 0) return 'Off';
    final hours = remainingSeconds ~/ 3600;
    final minutes = (remainingSeconds % 3600) ~/ 60;
    if (hours > 0) {
      return minutes > 0 ? '${hours}h ${minutes}m' : '${hours}h';
    }
    return '${minutes}m';
  }

  Future<void> _showSleepTimerSheet(
    BuildContext context,
    AudioPlayerNotifier audioNotifier,
    PlayerState playerState,
  ) async {
    final initialMinutes = playerState.sleepTimerRemainingSeconds > 0
        ? (playerState.sleepTimerRemainingSeconds / 60).clamp(5, 180).round()
        : 30;

    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: _surfaceGreyAlt,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(_radiusLarge)),
      ),
      builder: (sheetContext) {
        double sliderMinutes = initialMinutes.toDouble();
        return StatefulBuilder(
          builder: (context, setSheetState) {
            final formattedValue = _formatSleepTimerLabel(
              Duration(minutes: sliderMinutes.round()).inSeconds,
            );
            return SafeArea(
              top: false,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(20, 18, 20, 20),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'Sleep timer',
                      style: TextStyle(
                        color: Colors.white,
                        fontSize: 18,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      playerState.sleepTimerRemainingSeconds > 0
                          ? 'Currently ends in ${_formatSleepTimerLabel(playerState.sleepTimerRemainingSeconds)}.'
                          : 'Stop playback after a set time.',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.58),
                        fontSize: 13,
                      ),
                    ),
                    const SizedBox(height: 24),
                    Row(
                      children: [
                        Text(
                          'Off',
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.48),
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const Spacer(),
                        Text(
                          formattedValue,
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 16,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                    SliderTheme(
                      data: SliderTheme.of(context).copyWith(
                        activeTrackColor: _accentGrey,
                        inactiveTrackColor:
                            Colors.white.withValues(alpha: 0.12),
                        thumbColor: Colors.white,
                        overlayColor: Colors.white.withValues(alpha: 0.08),
                        trackHeight: 3,
                      ),
                      child: Slider(
                        min: 5,
                        max: 180,
                        divisions: 35,
                        value: sliderMinutes,
                        onChanged: (value) {
                          setSheetState(() => sliderMinutes = value);
                        },
                      ),
                    ),
                    const SizedBox(height: 12),
                    Row(
                      children: [
                        Expanded(
                          child: TextButton(
                            onPressed: () {
                              audioNotifier.setSleepTimer(null);
                              Navigator.of(sheetContext).pop();
                            },
                            child: const Text(
                              'Turn Off',
                              style: TextStyle(color: Colors.white70),
                            ),
                          ),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: TextButton(
                            onPressed: () {
                              audioNotifier.setSleepTimer(
                                Duration(minutes: sliderMinutes.round()),
                              );
                              Navigator.of(sheetContext).pop();
                            },
                            child: const Text(
                              'Set Timer',
                              style: TextStyle(color: Colors.white),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            );
          },
        );
      },
    );
  }

  bool _isTrackInQueue(PlaybackQueueState queueState, String? trackId) {
    if (trackId == null || trackId.isEmpty) return false;
    return queueState.queue.any((track) => extractTrackId(track) == trackId);
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
      unawaited(ref.read(lyricsProvider.notifier).fetchLyrics(videoId));
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

  int _resolvedQueueIndex(PlaybackQueueState queueState) {
    if (queueState.queue.isEmpty) return -1;
    final activeTrackId = queueState.currentTrackId;
    if (activeTrackId != null && activeTrackId.isNotEmpty) {
      final matchingIndex = queueState.queue.indexWhere(
        (track) => extractTrackId(track) == activeTrackId,
      );
      if (matchingIndex >= 0) {
        return matchingIndex;
      }
    }
    return queueState.currentIndex.clamp(0, queueState.queue.length - 1).toInt();
  }

  Map<String, dynamic>? _currentQueueTrack(PlaybackQueueState queueState) {
    final resolvedIndex = _resolvedQueueIndex(queueState);
    if (resolvedIndex < 0 || resolvedIndex >= queueState.queue.length) {
      return null;
    }
    return queueState.queue[resolvedIndex];
  }

  Map<String, dynamic>? _nextUpTrack(PlaybackQueueState queueState) {
    final resolvedIndex = _resolvedQueueIndex(queueState);
    for (var i = resolvedIndex + 1; i < queueState.queue.length; i++) {
      final track = queueState.queue[i];
      if (isTrackHidden(track)) {
        continue;
      }
      return track;
    }
    return queueState.recommendations.isNotEmpty
        ? queueState.recommendations.first
        : null;
  }

  Widget _buildPanelTab({
    required IconData icon,
    required VoidCallback onTap,
    required bool pointLeft,
  }) {
    return GestureDetector(
      onTap: onTap,
        child: ClipPath(
          clipper: _LyricsTabClipper(pointLeft: pointLeft),
          child: Container(
            width: 28,
            height: 74,
            decoration: BoxDecoration(
              color: _surfaceGreyAlt.withValues(alpha: 0.96),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.3),
                blurRadius: 18,
                offset: const Offset(0, 8),
              ),
            ],
          ),
          child: Center(
            child: Icon(
              icon,
              color: Colors.white.withValues(alpha: 0.82),
              size: 14,
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildQueueTile({
    required BuildContext context,
    required Map<String, dynamic> track,
    required bool isActive,
    required VoidCallback onTap,
    VoidCallback? onRemove,
    VoidCallback? onAddToPlaylist,
    String? trailingLabel,
    bool isDimmed = false,
    bool isRemoving = false,
    Widget? trailing,
  }) {
    final videoId = extractTrackId(track);
    final tile = Container(
      margin: const EdgeInsets.only(bottom: 10),
      decoration: BoxDecoration(
        color: isActive
            ? Colors.white.withValues(alpha: 0.08)
            : Colors.white.withValues(alpha: 0.03),
        borderRadius: BorderRadius.circular(_radiusLarge),
        border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
      ),
      child: AnimatedOpacity(
        duration: const Duration(milliseconds: 220),
        opacity: isDimmed ? 0.34 : 1,
        child: ListTile(
          contentPadding:
              const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
          onTap: onTap,
          leading: AppArtwork(
            thumbnail: track['thumbnail'],
            videoId: videoId,
            width: 58,
            height: 58,
            radius: _radiusMedium,
          ),
          title: Text(
            track['title'] ?? 'Unknown Track',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              color: isDimmed
                  ? Colors.white.withValues(alpha: 0.48)
                  : Colors.white,
              fontWeight: isActive ? FontWeight.w700 : FontWeight.w600,
            ),
          ),
          subtitle: Text(
            track['channel'] ?? track['author'] ?? '',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              color: Colors.white.withValues(alpha: isDimmed ? 0.3 : 0.54),
              fontSize: 12,
            ),
          ),
          trailing: trailing ??
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (trailingLabel != null) ...[
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 10,
                        vertical: 6,
                      ),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.08),
                        borderRadius: BorderRadius.circular(999),
                      ),
                      child: Text(
                        trailingLabel,
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.8),
                          fontSize: 11,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    const SizedBox(width: 6),
                  ],
                  if (onAddToPlaylist != null)
                    IconButton(
                      onPressed: onAddToPlaylist,
                      icon: Icon(
                        Icons.playlist_add_rounded,
                        color: Colors.white.withValues(alpha: 0.72),
                      ),
                    ),
                  if (onRemove != null)
                    IconButton(
                      onPressed: onRemove,
                      icon: Icon(
                        Icons.remove_circle_outline_rounded,
                        color: Colors.white.withValues(alpha: 0.62),
                      ),
                    ),
                ],
              ),
        ),
      ),
    );

    return TweenAnimationBuilder<double>(
      duration: const Duration(milliseconds: 260),
      curve: Curves.easeInOutCubic,
      tween: Tween<double>(begin: 1, end: isRemoving ? 0 : 1),
      builder: (context, value, child) {
        return ClipRect(
          child: Align(
            heightFactor: value.clamp(0, 1),
            child: Opacity(
              opacity: value.clamp(0, 1),
              child: Transform.translate(
                offset: Offset((1 - value) * -30, 0),
                child: child,
              ),
            ),
          ),
        );
      },
      child: tile,
    );
  }

  Widget _buildQueueSheet(
    BuildContext context,
    PlaybackQueueState queueState,
    PlaybackQueueNotifier queueNotifier,
    ScrollController scrollController,
    {required VoidCallback onDismiss}
  ) {
    final queueTitle = queueState.mode == PlaybackQueueMode.playlist
        ? (queueState.playlistName?.isNotEmpty == true
            ? queueState.playlistName!
            : 'Queue')
        : 'Queue';
    final header = GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onDismiss,
      child: Container(
        padding: const EdgeInsets.fromLTRB(22, 14, 22, 14),
        decoration: BoxDecoration(
          color: Colors.black.withValues(alpha: 0.96),
          borderRadius: const BorderRadius.vertical(
            top: Radius.circular(_radiusLarge),
          ),
          border: Border(
            bottom: BorderSide(color: Colors.white.withValues(alpha: 0.06)),
          ),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 42,
              height: 4,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.16),
                borderRadius: BorderRadius.circular(999),
              ),
            ),
            const SizedBox(height: 14),
            Row(
              children: [
                Text(
                  queueTitle,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );

    final resolvedCurrentIndex = _resolvedQueueIndex(queueState);
    final currentTrack = _currentQueueTrack(queueState);
    final earlierQueue = resolvedCurrentIndex > 0
        ? queueState.queue
            .take(resolvedCurrentIndex)
            .where((track) {
              final trackId = extractTrackId(track);
              if (trackId == null || trackId == queueState.currentTrackId) {
                return false;
              }
              return !queueState.playedTrackIds.contains(trackId);
            })
            .toList(growable: false)
        : const <Map<String, dynamic>>[];
    final playedQueue = queueState.queue
        .where((track) {
          final trackId = extractTrackId(track);
          if (trackId == null || trackId == queueState.currentTrackId) {
            return false;
          }
          return queueState.playedTrackIds.contains(trackId);
        })
        .toList(growable: false);
    final upcomingQueue = resolvedCurrentIndex >= 0 &&
            resolvedCurrentIndex + 1 < queueState.queue.length
        ? queueState.queue.sublist(resolvedCurrentIndex + 1)
        : const <Map<String, dynamic>>[];
    final queueChildren = <Widget>[];
    final searchQuery = _queueSearchController.text.trim();
    queueChildren.add(
      Container(
        margin: const EdgeInsets.only(bottom: 16),
        padding: const EdgeInsets.fromLTRB(14, 14, 14, 12),
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.03),
          borderRadius: BorderRadius.circular(_radiusLarge),
          border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: _queueSearchController,
              style: const TextStyle(color: Colors.white),
              decoration: InputDecoration(
                hintText: '',
                hintStyle: TextStyle(
                  color: Colors.white.withValues(alpha: 0.34),
                ),
                filled: true,
                fillColor: Colors.white.withValues(alpha: 0.04),
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 12,
                ),
                prefixIcon: Icon(
                  Icons.search_rounded,
                  color: Colors.white.withValues(alpha: 0.48),
                ),
                suffixIcon: _queueSearchController.text.trim().isEmpty
                    ? (_isQueueSearchLoading
                        ? const Padding(
                            padding: EdgeInsets.all(14),
                            child: SizedBox(
                              width: 16,
                              height: 16,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: _accentGrey,
                              ),
                            ),
                          )
                        : null)
                    : IconButton(
                        onPressed: _clearQueueSearch,
                        icon: Icon(
                          Icons.close_rounded,
                          color: Colors.white.withValues(alpha: 0.5),
                        ),
                      ),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(_radiusMedium),
                  borderSide: BorderSide.none,
                ),
              ),
            ),
            if (searchQuery.isNotEmpty ||
                _isQueueSearchLoading ||
                _queueSearchResults.isNotEmpty) ...[
              const SizedBox(height: 12),
              if (_queueSearchResults.isEmpty && _isQueueSearchLoading)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 12),
                  child: Center(
                    child: CircularProgressIndicator(color: _accentGrey),
                  ),
                )
              else if (_queueSearchResults.isEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                    'No songs found for "$searchQuery".',
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.46),
                      fontSize: 13,
                    ),
                  ),
                )
              else
                ..._queueSearchResults.take(6).map((track) {
                  final videoId = extractTrackId(track);
                  final queueIndex = videoId == null
                      ? -1
                      : queueState.queue.indexWhere(
                          (entry) => extractTrackId(entry) == videoId,
                        );
                  final isQueued = _isTrackInQueue(queueState, videoId);
                  final isCurrent =
                      videoId != null && videoId == queueState.currentTrackId;
                  return _buildQueueTile(
                    context: context,
                    track: track,
                    isActive: isCurrent,
                    onTap: () {
                      if (isQueued) {
                        unawaited(queueNotifier.playQueueIndex(queueIndex));
                        return;
                      }
                      unawaited(queueNotifier.enqueueTrack(track));
                    },
                    trailing: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        if (isQueued)
                          Container(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 10,
                              vertical: 6,
                            ),
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.08),
                              borderRadius: BorderRadius.circular(999),
                            ),
                            child: Text(
                              isCurrent ? 'Playing' : 'In Queue',
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.8),
                                fontSize: 11,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          )
                        else
                          IconButton(
                            onPressed: () =>
                                unawaited(queueNotifier.enqueueTrack(track)),
                            icon: Icon(
                              Icons.add_circle_outline_rounded,
                              color: Colors.white.withValues(alpha: 0.74),
                            ),
                          ),
                      ],
                    ),
                  );
                }),
            ],
          ],
        ),
      ),
    );

    if (earlierQueue.isNotEmpty) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 2, 6, 12),
          child: Text(
            'Queued earlier',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.54),
              fontSize: 12,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.4,
            ),
          ),
        ),
      );
      for (final track in earlierQueue) {
        final videoId = extractTrackId(track);
        final queueIndex = videoId == null
            ? -1
            : queueState.queue.indexWhere(
                (entry) => extractTrackId(entry) == videoId,
              );
        queueChildren.add(
          _buildQueueTile(
            context: context,
            track: track,
            isActive: false,
            onTap: queueIndex >= 0
                ? () => unawaited(queueNotifier.playQueueIndex(queueIndex))
                : () {},
          ),
        );
      }
    }

    if (playedQueue.isNotEmpty) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 2, 6, 12),
          child: Text(
            'Played',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.54),
              fontSize: 12,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.4,
            ),
          ),
        ),
      );
      for (final track in playedQueue) {
        final videoId = extractTrackId(track);
        final queueIndex = videoId == null
            ? -1
            : queueState.queue.indexWhere(
                (entry) => extractTrackId(entry) == videoId,
              );
        queueChildren.add(
          _buildQueueTile(
            context: context,
            track: track,
            isActive: false,
            isDimmed: true,
            onTap: queueIndex >= 0
                ? () => unawaited(queueNotifier.playQueueIndex(queueIndex))
                : () {},
          ),
        );
      }
    }

    if (currentTrack != null) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: _buildQueueTile(
            context: context,
            track: currentTrack,
            isActive: true,
            onTap: () => unawaited(queueNotifier.playQueueIndex(resolvedCurrentIndex)),
            trailingLabel: 'Playing',
          ),
        ),
      );
    }

    if (upcomingQueue.isNotEmpty) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 2, 6, 12),
          child: Text(
            'Up next',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.72),
              fontSize: 13,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.45,
            ),
          ),
        ),
      );
    }

    if (upcomingQueue.isNotEmpty) {
      queueChildren.add(
        ReorderableListView.builder(
          shrinkWrap: true,
          buildDefaultDragHandles: false,
          physics: const NeverScrollableScrollPhysics(),
          itemCount: upcomingQueue.length,
          onReorder: queueNotifier.reorderUpcomingQueue,
          proxyDecorator: (child, index, animation) {
            return AnimatedBuilder(
              animation: animation,
              builder: (context, _) {
                final elevation =
                    Tween<double>(begin: 0, end: 16).transform(animation.value);
                return Material(
                  color: Colors.transparent,
                  elevation: elevation,
                  child: child,
                );
              },
            );
          },
          itemBuilder: (context, index) {
            final track = upcomingQueue[index];
            final videoId = extractTrackId(track);
            final isHidden = isTrackHidden(track);
            final isRemoving =
                videoId != null && _removingQueueTrackIds.contains(videoId);
            final actualQueueIndex = resolvedCurrentIndex + 1 + index;
            return ReorderableDelayedDragStartListener(
              key: ValueKey(
                'queue-upcoming-${videoId ?? '$actualQueueIndex-${track['title']}'}',
              ),
              index: index,
              child: _buildQueueTile(
                context: context,
                track: track,
                isActive: false,
                isDimmed: isHidden,
                isRemoving: isRemoving,
                onTap: () =>
                    unawaited(queueNotifier.playQueueIndex(actualQueueIndex)),
                trailing: videoId != null
                    ? Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          IconButton(
                            onPressed: () {
                              if (queueState.mode == PlaybackQueueMode.playlist &&
                                  queueState.playlistId != null) {
                                ref.read(playlistProvider.notifier).setTrackHidden(
                                      queueState.playlistId!,
                                      videoId,
                                      !isHidden,
                                    );
                              }
                              queueNotifier.setQueueTrackHidden(
                                videoId,
                                !isHidden,
                              );
                            },
                            icon: AnimatedRotation(
                              duration: const Duration(milliseconds: 220),
                              turns: isHidden ? 0.08 : 0,
                              child: Icon(
                                isHidden
                                    ? Icons.visibility_off_rounded
                                    : Icons.visibility_rounded,
                                color: Colors.white.withValues(
                                  alpha: isHidden ? 0.38 : 0.7,
                                ),
                              ),
                            ),
                          ),
                          if (queueState.mode == PlaybackQueueMode.radio)
                            IconButton(
                              onPressed: () => showAddToPlaylistDialog(
                                context: context,
                                track: track,
                              ),
                              icon: Icon(
                                Icons.playlist_add_rounded,
                                color: Colors.white.withValues(alpha: 0.72),
                              ),
                            ),
                          IconButton(
                            onPressed: () {
                              unawaited(
                                _animateQueueRemoval(videoId, () {
                                  if (queueState.mode ==
                                          PlaybackQueueMode.playlist &&
                                      queueState.playlistId != null) {
                                    ref
                                        .read(playlistProvider.notifier)
                                        .removeTrackFromPlaylist(
                                          queueState.playlistId!,
                                          videoId,
                                        );
                                    queueNotifier.removePlaylistTrack(videoId);
                                    return;
                                  }
                                  queueNotifier.removeFromQueue(videoId);
                                }),
                              );
                            },
                            icon: Icon(
                              queueState.mode == PlaybackQueueMode.playlist
                                  ? Icons.delete_outline_rounded
                                  : Icons.remove_circle_outline_rounded,
                              color: Colors.white.withValues(alpha: 0.58),
                            ),
                          ),
                        ],
                      )
                    : null,
              ),
            );
          },
        ),
      );
    } else if (currentTrack != null) {
      queueChildren.add(
        Padding(
          padding: const EdgeInsets.fromLTRB(6, 4, 6, 18),
          child: Text(
            queueState.mode == PlaybackQueueMode.radio
                ? 'This queue will refill after the current song finishes.'
                : 'No upcoming songs are queued after the current track yet.',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.46),
              fontSize: 12.5,
              height: 1.5,
            ),
          ),
        ),
      );
    }

    if (queueState.mode == PlaybackQueueMode.radio && queueState.isLoadingQueue) {
      queueChildren.add(
        const Padding(
          padding: EdgeInsets.symmetric(vertical: 18),
          child: Center(
            child: CircularProgressIndicator(color: _accentGrey),
          ),
        ),
      );
    }

    if (queueState.mode == PlaybackQueueMode.playlist &&
        (queueState.recommendations.isNotEmpty ||
            queueState.isLoadingRecommendations)) {
      queueChildren.add(
        const Padding(
          padding: EdgeInsets.fromLTRB(6, 18, 6, 12),
          child: Text(
            'Based on this song',
            style: TextStyle(
              color: Colors.white,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
          ),
        ),
      );
      if (queueState.recommendations.isEmpty &&
          queueState.isLoadingRecommendations) {
        queueChildren.add(
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 18),
            child: Center(
              child: CircularProgressIndicator(color: _accentGrey),
            ),
          ),
        );
      } else {
        for (final track in queueState.recommendations) {
          queueChildren.add(
            _buildQueueTile(
              context: context,
              track: track,
              isActive: false,
              onTap: () => unawaited(queueNotifier.startRadioSession(track)),
              onAddToPlaylist: () => showAddToPlaylistDialog(
                context: context,
                track: track,
              ),
            ),
          );
        }
      }
      if (queueState.isLoadingRecommendations &&
          queueState.recommendations.isNotEmpty) {
        queueChildren.add(
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 18),
            child: Center(
              child: CircularProgressIndicator(color: _accentGrey),
            ),
          ),
        );
      }
    }

    return Container(
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.92),
        borderRadius: const BorderRadius.vertical(
          top: Radius.circular(_radiusLarge),
        ),
        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.38),
            blurRadius: 28,
            offset: const Offset(0, -8),
          ),
        ],
      ),
      child: Column(
        children: [
          header,
          Expanded(
            child: queueState.mode == PlaybackQueueMode.none
                ? const Center(
                    child: Padding(
                      padding: EdgeInsets.symmetric(horizontal: 28),
                      child: Text(
                        'Start a song from search, recommendations, or a playlist and the live queue will appear here.',
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: Colors.white54,
                          fontSize: 15,
                          height: 1.45,
                        ),
                      ),
                    ),
                  )
                : NotificationListener<ScrollNotification>(
                    onNotification: (notification) {
                      if (notification.metrics.pixels >=
                          notification.metrics.maxScrollExtent - 180) {
                        unawaited(queueNotifier.loadMore());
                      }
                      return false;
                    },
                    child: ListView(
                      controller: scrollController,
                      physics: const BouncingScrollPhysics(
                        parent: AlwaysScrollableScrollPhysics(),
                      ),
                      padding: EdgeInsets.fromLTRB(
                        16,
                        8,
                        16,
                        math.max(
                          140,
                          MediaQuery.of(context).padding.bottom + 120,
                        ).toDouble(),
                      ),
                      children: queueChildren,
                    ),
                  ),
          ),
        ],
      ),
    );
  }

  Widget _buildLyricsPanel(
    BuildContext context,
    PlayerState playerState,
    TrackLyricsState lyricsState,
  ) {
    final activeIndex = _activeLyricsIndex(lyricsState, playerState);
    _syncLyricsScroll(activeIndex);

    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(18, 94, 18, 28),
        child: Container(
          decoration: BoxDecoration(
            color: const Color(0xFF101216).withValues(alpha: 0.94),
            borderRadius: BorderRadius.circular(_radiusLarge),
            border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.34),
                blurRadius: 30,
                offset: const Offset(0, 18),
              ),
            ],
          ),
          child: Padding(
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
                if (lyricsState.source != null && lyricsState.source!.isNotEmpty)
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
                    return const Center(
                      key: ValueKey('lyrics-loading'),
                      child: CircularProgressIndicator(color: _accentGrey),
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
                        key: _lyricKeyFor(index),
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
                              fontWeight:
                                  isActive ? FontWeight.w700 : FontWeight.w500,
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
        ),
      ),
    );
  }

  Widget _buildPlayerPanel(
    BuildContext context,
    PlayerState playerState,
    AudioPlayerNotifier audioNotifier,
    PlaybackQueueState queueState,
  ) {
    const accentColor = _accentGrey;
    final nextUpTrack = _nextUpTrack(queueState);
    final queueNotifier = ref.read(playbackQueueProvider.notifier);
    final mediaQuery = MediaQuery.of(context);
    final bottomInset = mediaQuery.padding.bottom > mediaQuery.viewPadding.bottom
        ? mediaQuery.padding.bottom
        : mediaQuery.viewPadding.bottom;
    final downloadTask = playerState.videoId == null
        ? null
        : ref.watch(downloadTaskProvider(playerState.videoId!));

    return GestureDetector(
      behavior: HitTestBehavior.translucent,
      onVerticalDragEnd: (details) {
        final velocity = details.primaryVelocity ?? 0;
        if (velocity > 700) {
          if (Navigator.canPop(context)) {
            Navigator.pop(context);
          }
        } else if (velocity < -700) {
          _openQueueSheet();
        }
      },
      child: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) {
            final compactLayout = constraints.maxHeight < 760;
            final extraCompactLayout = constraints.maxHeight < 690;
            final hasQueuePreview = nextUpTrack != null;
            final queueAwareCompactLayout = compactLayout && hasQueuePreview;
            final artworkSize = math.min(
              mediaQuery.size.width *
                  (queueAwareCompactLayout
                      ? 0.52
                      : compactLayout
                          ? 0.62
                          : 0.76),
              queueAwareCompactLayout
                  ? 244.0
                  : compactLayout
                      ? 296.0
                      : 360.0,
            ).clamp(
                    queueAwareCompactLayout
                        ? 164.0
                        : extraCompactLayout
                            ? 182.0
                            : 214.0,
                    360.0)
                .toDouble();
            final titleSize = queueAwareCompactLayout
                ? 22.0
                : compactLayout
                    ? 24.0
                    : 28.0;
            final artistSize = queueAwareCompactLayout
                ? 15.0
                : compactLayout
                    ? 16.0
                    : 18.0;
            final artworkGap = queueAwareCompactLayout
                ? 16.0
                : compactLayout
                    ? 24.0
                    : 40.0;
            final cardGap = queueAwareCompactLayout ? 10.0 : compactLayout ? 14.0 : 18.0;
            final sliderGap = queueAwareCompactLayout ? 12.0 : compactLayout ? 18.0 : 26.0;
            final toolRowGap = queueAwareCompactLayout ? 10.0 : compactLayout ? 12.0 : 20.0;
            final transportIconSize = queueAwareCompactLayout
                ? 28.0
                : compactLayout
                    ? 30.0
                    : 34.0;
            final transportButtonWidth = queueAwareCompactLayout
                ? 42.0
                : compactLayout
                    ? 46.0
                    : 50.0;
            final primaryTransportSize = queueAwareCompactLayout
                ? 68.0
                : compactLayout
                    ? 74.0
                    : 84.0;
            final primaryTransportBox = queueAwareCompactLayout
                ? 76.0
                : compactLayout
                    ? 82.0
                    : 92.0;
            return SingleChildScrollView(
              physics: const BouncingScrollPhysics(
                parent: AlwaysScrollableScrollPhysics(),
              ),
              padding: EdgeInsets.fromLTRB(
                24,
                18,
                24,
                math.max(compactLayout ? 161 : 201, bottomInset + 109)
                    .toDouble(),
              ),
              child: ConstrainedBox(
                constraints: BoxConstraints(minHeight: constraints.maxHeight),
                child: Transform.translate(
                  offset: const Offset(0, -5),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                    const SizedBox(height: 8),
                    Center(
                      child: Hero(
                        tag: 'album_art_${playerState.currentTrackName}',
                        child: Container(
                          padding: const EdgeInsets.all(10),
                          decoration: BoxDecoration(
                            borderRadius: BorderRadius.circular(_radiusLarge),
                            border: Border.all(
                              color: Colors.white.withValues(alpha: 0.08),
                            ),
                            color: Colors.white.withValues(alpha: 0.03),
                            boxShadow: [
                              BoxShadow(
                                color: Colors.black.withValues(alpha: 0.44),
                                blurRadius: 48,
                                offset: const Offset(0, 26),
                              ),
                            ],
                          ),
                          child: AppArtwork(
                            thumbnail: playerState.thumbnail,
                            videoId: playerState.videoId,
                            width: artworkSize,
                            height: artworkSize,
                            radius: _radiusMedium,
                          ),
                        ),
                      ),
                    ),
                    SizedBox(height: artworkGap),
                    Text(
                      playerState.currentTrackName,
                      style: TextStyle(
                        fontSize: titleSize,
                        fontWeight: FontWeight.w800,
                        color: Colors.white,
                        height: 1.2,
                      ),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                    const SizedBox(height: 8),
                    Text(
                      playerState.artist ?? 'Artist Unknown',
                      style: TextStyle(
                        fontSize: artistSize,
                        color: Colors.white.withValues(alpha: 0.7),
                      ),
                    ),
                    SizedBox(height: nextUpTrack != null ? cardGap : 0),
                    AnimatedSwitcher(
                      duration: const Duration(milliseconds: 280),
                      switchInCurve: Curves.easeOutCubic,
                      switchOutCurve: Curves.easeInCubic,
                      transitionBuilder: (child, animation) {
                        final curved = CurvedAnimation(
                          parent: animation,
                          curve: Curves.easeOutCubic,
                          reverseCurve: Curves.easeInCubic,
                        );
                        return FadeTransition(
                          opacity: curved,
                          child: SlideTransition(
                            position: Tween<Offset>(
                              begin: const Offset(0, 0.08),
                              end: Offset.zero,
                            ).animate(curved),
                            child: child,
                          ),
                        );
                      },
                      child: nextUpTrack == null
                          ? const SizedBox.shrink(
                              key: ValueKey('player-next-up-empty'),
                            )
                          : GestureDetector(
                              key: ValueKey(
                                'player-next-up-${extractTrackId(nextUpTrack) ?? nextUpTrack['title'] ?? 'unknown'}',
                              ),
                              onTap: _openQueueSheet,
                              child: Container(
                                width: double.infinity,
                                padding: EdgeInsets.symmetric(
                                  horizontal: 14,
                                  vertical: queueAwareCompactLayout ? 10 : 14,
                                ),
                                decoration: BoxDecoration(
                                  color: Colors.white.withValues(alpha: 0.04),
                                  borderRadius:
                                      BorderRadius.circular(_radiusLarge),
                                  border: Border.all(
                                    color: Colors.white.withValues(alpha: 0.08),
                                  ),
                                ),
                                child: Row(
                                  children: [
                                    Icon(
                                      Icons.queue_music_rounded,
                                      color: Colors.white.withValues(alpha: 0.72),
                                      size: 18,
                                    ),
                                    const SizedBox(width: 12),
                                    Expanded(
                                      child: queueAwareCompactLayout
                                          ? Text(
                                              nextUpTrack['title'] ??
                                                  'More like this',
                                              maxLines: 1,
                                              overflow: TextOverflow.ellipsis,
                                              style: const TextStyle(
                                                color: Colors.white,
                                                fontSize: 13,
                                                fontWeight: FontWeight.w600,
                                              ),
                                            )
                                          : Column(
                                              crossAxisAlignment:
                                                  CrossAxisAlignment.start,
                                              children: [
                                                Text(
                                                  'Up next',
                                                  style: TextStyle(
                                                    color: Colors.white.withValues(
                                                      alpha: 0.46,
                                                    ),
                                                    fontSize: 11,
                                                    fontWeight: FontWeight.w700,
                                                    letterSpacing: 0.5,
                                                  ),
                                                ),
                                                const SizedBox(height: 4),
                                                Text(
                                                  nextUpTrack['title'] ??
                                                      'More like this',
                                                  maxLines: 1,
                                                  overflow:
                                                      TextOverflow.ellipsis,
                                                  style: const TextStyle(
                                                    color: Colors.white,
                                                    fontSize: 14,
                                                    fontWeight: FontWeight.w600,
                                                  ),
                                                ),
                                              ],
                                            ),
                                    ),
                                    Icon(
                                      Icons.keyboard_arrow_up_rounded,
                                      color: Colors.white.withValues(alpha: 0.56),
                                    ),
                                  ],
                                ),
                              ),
                            ),
                    ),
                    SizedBox(height: sliderGap),
                    SliderTheme(
                      data: SliderTheme.of(context).copyWith(
                        activeTrackColor: Colors.white,
                        inactiveTrackColor: Colors.white.withValues(alpha: 0.2),
                        thumbColor: Colors.white,
                        overlayColor: Colors.white.withValues(alpha: 0.1),
                        trackHeight: 4.0,
                        thumbShape: const RoundSliderThumbShape(
                          enabledThumbRadius: 6,
                        ),
                      ),
                      child: Slider(
                        value: playerState.duration > 0
                            ? _dragValue ??
                                (playerState.currentPosition /
                                        playerState.duration)
                                    .clamp(0.0, 1.0)
                            : 0.0,
                        onChanged: playerState.duration > 0
                            ? (val) => setState(() => _dragValue = val)
                            : null,
                        onChangeEnd: playerState.duration > 0
                            ? (val) {
                                unawaited(
                                  audioNotifier.seek(
                                    (val * playerState.duration).toInt(),
                                  ),
                                );
                                setState(() => _dragValue = null);
                              }
                            : null,
                      ),
                    ),
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 8.0),
                      child: Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Text(
                            '${(playerState.currentPosition / 60).floor()}:${(playerState.currentPosition % 60).toString().padLeft(2, '0')}',
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.6),
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          Text(
                            '${(playerState.duration / 60).floor()}:${(playerState.duration % 60).toString().padLeft(2, '0')}',
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.6),
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ],
                      ),
                    ),
                    SizedBox(height: toolRowGap),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        IconButton(
                          visualDensity: VisualDensity.compact,
                          iconSize: 28,
                          padding: EdgeInsets.zero,
                          icon: Icon(
                            Icons.shuffle_rounded,
                            color: Colors.white.withValues(alpha: 0.5),
                          ),
                          onPressed: () {
                            unawaited(
                              ref.read(playbackQueueProvider.notifier).playShuffled(),
                            );
                          },
                        ),
                        Stack(
                          clipBehavior: Clip.none,
                          children: [
                            IconButton(
                              visualDensity: VisualDensity.compact,
                              iconSize: 28,
                              padding: EdgeInsets.zero,
                              icon: Icon(
                                Icons.timer_outlined,
                                color: playerState.sleepTimerRemainingSeconds > 0
                                    ? accentColor
                                    : Colors.white.withValues(alpha: 0.5),
                              ),
                              onPressed: () => _showSleepTimerSheet(
                                context,
                                audioNotifier,
                                playerState,
                              ),
                            ),
                            if (playerState.sleepTimerRemainingSeconds > 0)
                              Positioned(
                                right: -2,
                                top: 2,
                                child: Container(
                                  padding: const EdgeInsets.symmetric(
                                    horizontal: 5,
                                    vertical: 2,
                                  ),
                                  decoration: BoxDecoration(
                                    color: Colors.white.withValues(alpha: 0.14),
                                    borderRadius: BorderRadius.circular(999),
                                  ),
                                  child: Text(
                                    _formatSleepTimerLabel(
                                      playerState.sleepTimerRemainingSeconds,
                                    ),
                                    style: const TextStyle(
                                      color: Colors.white,
                                      fontSize: 9,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                ),
                              ),
                          ],
                        ),
                        IconButton(
                          visualDensity: VisualDensity.compact,
                          iconSize: 28,
                          padding: EdgeInsets.zero,
                          icon: Icon(
                            Icons.loop,
                            color: playerState.isLooping
                                ? accentColor
                                : Colors.white.withValues(alpha: 0.5),
                          ),
                          onPressed: () {
                            audioNotifier.toggleLoop(
                              0,
                              playerState.duration * 1000,
                            );
                          },
                        ),
                        IconButton(
                          visualDensity: VisualDensity.compact,
                          iconSize: 28,
                          padding: EdgeInsets.zero,
                          tooltip: 'Download',
                          icon: downloadTask?.phase == DownloadPhase.active
                              ? SizedBox(
                                  width: 24,
                                  height: 24,
                                  child: CircularProgressIndicator(
                                    value: downloadTask!.progress > 0
                                        ? downloadTask.progress
                                        : null,
                                    strokeWidth: 2.3,
                                    color: accentColor,
                                  ),
                                )
                              : Icon(
                                  downloadTask?.phase == DownloadPhase.complete
                                      ? Icons.download_done_rounded
                                      : Icons.download_rounded,
                                  color: downloadTask?.phase ==
                                          DownloadPhase.complete
                                      ? accentColor
                                      : Colors.white.withValues(alpha: 0.5),
                                ),
                          onPressed: playerState.videoId == null
                              ? null
                              : () {
                                  ref
                                      .read(downloadCenterProvider.notifier)
                                      .downloadTrack({
                                    'id': playerState.videoId,
                                    'videoId': playerState.videoId,
                                    'title': playerState.currentTrackName,
                                    'thumbnail': playerState.thumbnail,
                                    'channel': playerState.artist,
                                    'duration': playerState.duration,
                                  });
                                },
                        ),
                        IconButton(
                          visualDensity: VisualDensity.compact,
                          iconSize: 28,
                          padding: EdgeInsets.zero,
                          icon: Icon(
                            Icons.playlist_add,
                            color: Colors.white.withValues(alpha: 0.5),
                          ),
                          onPressed: () {
                            if (playerState.videoId == null) return;
                            showAddToPlaylistDialog(
                              context: context,
                              track: {
                                'id': playerState.videoId,
                                'videoId': playerState.videoId,
                                'title': playerState.currentTrackName,
                                'thumbnail': playerState.thumbnail,
                                'channel': playerState.artist,
                                'duration': playerState.duration,
                              },
                            );
                          },
                        ),
                      ],
                    ),
                    SizedBox(height: compactLayout ? 12 : 18),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                      crossAxisAlignment: CrossAxisAlignment.center,
                      children: [
                        SizedBox(
                          width: transportButtonWidth,
                          child: IconButton(
                            visualDensity: VisualDensity.compact,
                            iconSize: transportIconSize,
                            padding: EdgeInsets.zero,
                            onPressed: () {
                              unawaited(queueNotifier.playPrevious());
                            },
                            icon: Icon(
                              Icons.skip_previous_rounded,
                              color: Colors.white.withValues(alpha: 0.82),
                            ),
                          ),
                        ),
                        SizedBox(
                          width: transportButtonWidth,
                          child: IconButton(
                            visualDensity: VisualDensity.compact,
                            iconSize: transportIconSize,
                            padding: EdgeInsets.zero,
                            onPressed: () {
                              unawaited(
                                audioNotifier.seek(
                                  playerState.currentPosition - 10,
                                ),
                              );
                            },
                            icon: Icon(
                              Icons.replay_10_rounded,
                              color: Colors.white.withValues(alpha: 0.8),
                            ),
                          ),
                        ),
                        SizedBox(
                          width: primaryTransportBox,
                          child: Center(
                            child: GestureDetector(
                              onTap: () {
                                unawaited(
                                  playerState.isPlaying
                                      ? audioNotifier.pause()
                                      : audioNotifier.play(),
                                );
                              },
                              child: Container(
                                width: primaryTransportSize,
                                height: primaryTransportSize,
                                decoration: BoxDecoration(
                                  shape: BoxShape.circle,
                                  color: _surfaceGreyAlt,
                                  boxShadow: [
                                    BoxShadow(
                                      color: Colors.black.withValues(alpha: 0.26),
                                      blurRadius: 24,
                                      offset: const Offset(0, 10),
                                    ),
                                  ],
                                ),
                                alignment: Alignment.center,
                                child: playerState.isDownloading
                                    ? const SizedBox(
                                        width: 38,
                                        height: 38,
                                        child: CircularProgressIndicator(
                                          color: Colors.white,
                                          strokeWidth: 3,
                                        ),
                                      )
                                    : Transform.translate(
                                        offset: Offset(
                                          playerState.isPlaying ? 0 : 1,
                                          0,
                                        ),
                                        child: Icon(
                                          playerState.isPlaying
                                              ? Icons.pause_rounded
                                              : Icons.play_arrow_rounded,
                                          color: Colors.white,
                                          size: 42,
                                        ),
                                      ),
                              ),
                            ),
                          ),
                        ),
                        SizedBox(
                          width: transportButtonWidth,
                          child: IconButton(
                            visualDensity: VisualDensity.compact,
                            iconSize: transportIconSize,
                            padding: EdgeInsets.zero,
                            onPressed: () {
                              unawaited(
                                audioNotifier.seek(
                                  playerState.currentPosition + 10,
                                ),
                              );
                            },
                            icon: Icon(
                              Icons.forward_10_rounded,
                              color: Colors.white.withValues(alpha: 0.8),
                            ),
                          ),
                        ),
                        SizedBox(
                          width: transportButtonWidth,
                          child: IconButton(
                            visualDensity: VisualDensity.compact,
                            iconSize: transportIconSize,
                            padding: EdgeInsets.zero,
                            onPressed: () {
                              unawaited(queueNotifier.playNext());
                            },
                            icon: Icon(
                              Icons.skip_next_rounded,
                              color: Colors.white.withValues(alpha: 0.82),
                            ),
                          ),
                        ),
                      ],
                    ),
                    SizedBox(height: compactLayout ? 10 : 14),
                  ],
                ),
                ),
              ),
            );
          },
        ),
      ),
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
        title: const Text(
          'Now Playing',
          style: TextStyle(
            fontSize: 11,
            color: Colors.white,
            fontWeight: FontWeight.w700,
            letterSpacing: 1.4,
          ),
        ),
        centerTitle: true,
      ),
      body: Stack(
        fit: StackFit.expand,
        children: [
          const ColoredBox(color: _voidBlack),
          PageView(
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
          Positioned(
            right: _activePage == 0 ? 0 : null,
            left: _activePage == 1 ? 0 : null,
            top: MediaQuery.of(context).size.height * 0.22,
            child: _buildPanelTab(
              icon: _activePage == 0
                  ? Icons.lyrics_rounded
                  : Icons.graphic_eq_rounded,
              pointLeft: _activePage == 0,
              onTap: _activePage == 0 ? _openLyricsPanel : _closeLyricsPanel,
            ),
          ),
        ],
      ),
    );
  }
}

