part of 'main.dart';

class HomeScreen extends ConsumerStatefulWidget {
  const HomeScreen({super.key});

  @override
  ConsumerState<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends ConsumerState<HomeScreen> {
  final TextEditingController _urlController = TextEditingController();
  final FocusNode _searchFocusNode = FocusNode();
  final ScrollController _homeScrollController = ScrollController();
  bool _isSearching = false;
  bool _refreshRecommendationsOnSearchExit = false;
  String _lastSearchRecommendationQuery = '';
  List<String> _lastSearchRecommendationArtistHints = const <String>[];
  List<Map<String, dynamic>> _lastSearchRecommendationAnchorTracks =
      const <Map<String, dynamic>>[];
  bool _isClearingSearch = false;
  Timer? _suggestDebounce;
  final Set<String> _prewarmedTrackIds = <String>{};
  final Set<String> _pendingLastPlayedRemovals = <String>{};
  final Set<String> _hiddenLastPlayedTrackIds = <String>{};
  String _lastPrimeSignature = '';
  late final String _heroQuip;

  @override
  void initState() {
    super.initState();
    _heroQuip = _buildHeroQuip();
    _homeScrollController.addListener(_handleHomeScroll);
    _urlController.addListener(() {
      if (_isClearingSearch) {
        return;
      }
      final text = _urlController.text.trim();
      if (mounted) {
        setState(() {});
      }
      if (text.isNotEmpty && !_isSearching) {
        _suggestDebounce?.cancel();
        _suggestDebounce = Timer(const Duration(milliseconds: 280), () {
          if (mounted) {
            ref.read(suggestProvider.notifier).fetchSuggestions(text);
          }
        });
      } else if (text.isEmpty) {
        _suggestDebounce?.cancel();
        if (_isSearching || _refreshRecommendationsOnSearchExit) {
          _clearSearch();
          return;
        }
        ref.read(suggestProvider.notifier).clear();
        ref.read(searchPageProvider.notifier).clear();
        setState(() => _isSearching = false);
      }
    });
  }

  @override
  void dispose() {
    _suggestDebounce?.cancel();
    _homeScrollController
      ..removeListener(_handleHomeScroll)
      ..dispose();
    _urlController.dispose();
    _searchFocusNode.dispose();
    super.dispose();
  }

  void _handleHomeScroll() {
    if (_isSearching || !_homeScrollController.hasClients) return;
    final position = _homeScrollController.position;
    if (position.extentAfter > 900) return;
    final recState = ref.read(recommendationProvider);
    final nextRow = recState.rows.firstWhere(
      (row) =>
          row.kind == 'quiet_picks' &&
          row.hasMore &&
          !ref.read(recommendationProvider.notifier).isRowPaginating(row.id),
      orElse: () => const RecommendationFeedRowState(
        id: '',
        title: '',
        kind: '',
        itemType: 'track',
        items: [],
        nextOffset: 0,
        hasMore: false,
      ),
    );
    if (nextRow.id.isNotEmpty) {
      unawaited(
          ref.read(recommendationProvider.notifier).loadMoreRow(nextRow.id));
    }
  }

  bool _handleHomeScrollNotification(ScrollNotification notification) {
    if (notification.metrics.axis == Axis.vertical) {
      _handleHomeScroll();
    }
    return false;
  }

  void _ensureRecommendationPageCanPaginate(
    RecommendationFeedState recState,
    bool isRecLoading,
    bool isRecPaginating,
  ) {
    if (_isSearching || isRecLoading || isRecPaginating) return;
    if (!recState.rows.any((row) => row.kind == 'quiet_picks' && row.hasMore)) {
      return;
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _handleHomeScroll();
    });
  }

  List<String> _deriveArtistHintsFromTracks(Iterable<dynamic> tracks) {
    final seen = <String>{};
    final hints = <String>[];
    for (final track in tracks) {
      for (final artist in extractTrackArtists(track)) {
        final normalized = artist.trim();
        if (normalized.isEmpty) continue;
        final key = normalized.toLowerCase();
        if (!seen.add(key)) continue;
        hints.add(normalized);
        if (hints.length >= 6) {
          return hints;
        }
      }
    }
    return hints;
  }

  String _homeTrackKey(Map<String, dynamic> track) {
    final trackId = extractTrackId(track)?.trim() ?? '';
    if (trackId.isNotEmpty) {
      return 'track:$trackId';
    }
    final title = track['title']?.toString().trim().toLowerCase() ?? '';
    final artist = (track['channel'] ?? track['author'] ?? track['artist'])
            ?.toString()
            .trim()
            .toLowerCase() ??
        '';
    return 'track:$title|$artist';
  }

  bool _isLastPlayedDeleting(Map<String, dynamic> track) {
    final trackId = extractTrackId(track)?.trim() ?? '';
    return trackId.isNotEmpty && _pendingLastPlayedRemovals.contains(trackId);
  }

  List<Map<String, dynamic>> _visibleLastPlayedTracks(
    List<Map<String, dynamic>> tracks,
  ) {
    if (_hiddenLastPlayedTrackIds.isEmpty) {
      return tracks;
    }
    return tracks
        .where((track) {
          final trackId = extractTrackId(track)?.trim() ?? '';
          return trackId.isEmpty ||
              !_hiddenLastPlayedTrackIds.contains(trackId);
        })
        .map((track) => Map<String, dynamic>.from(track))
        .toList(growable: false);
  }

  List<Map<String, dynamic>> _deriveSearchAnchorTracks(
    Iterable<dynamic> tracks, {
    int limit = 4,
  }) {
    final anchors = <Map<String, dynamic>>[];
    final seen = <String>{};
    for (final rawTrack in tracks) {
      if (rawTrack is! Map) continue;
      final track = normalizeTrack(Map<String, dynamic>.from(rawTrack));
      final key = _homeTrackKey(track);
      if (key.trim().isEmpty || !seen.add(key)) continue;
      anchors.add(track);
      if (anchors.length >= limit) {
        break;
      }
    }
    return anchors;
  }

  List<String> _deriveSearchArtistHints({
    required Iterable<dynamic> trackResults,
    required Iterable<Map<String, dynamic>> artistResults,
  }) {
    final seen = <String>{};
    final hints = <String>[];

    void addHint(String? rawValue) {
      final value = rawValue?.trim() ?? '';
      if (value.isEmpty) return;
      final normalized = value.toLowerCase();
      if (!seen.add(normalized)) return;
      hints.add(value);
    }

    for (final track in trackResults) {
      for (final artist in extractTrackArtists(track)) {
        addHint(artist);
        if (hints.length >= 6) {
          return hints;
        }
      }
    }
    for (final artist in artistResults.take(4)) {
      addHint(artist['name']?.toString());
      if (hints.length >= 6) {
        break;
      }
    }
    return hints;
  }

  String _normalizeSearchValue(String? value) {
    return value?.trim().toLowerCase() ?? '';
  }

  Map<String, dynamic>? _buildAnchorFeaturedArtist(
    List<Map<String, dynamic>> anchorTracks,
  ) {
    if (anchorTracks.isEmpty) {
      return null;
    }
    final primaryTrack = anchorTracks.first;
    final artistNames = extractTrackArtists(primaryTrack);
    final artistName = artistNames.isEmpty ? '' : artistNames.first.trim();
    if (artistName.isEmpty) {
      return null;
    }
    return {
      'id': '',
      'name': artistName,
      'thumbnail': primaryTrack['thumbnail'],
      'description': 'Primary artist from your top search match.',
    };
  }

  Map<String, dynamic>? _resolveFeaturedSearchArtist({
    required String query,
    required List<dynamic> trackResults,
    required List<Map<String, dynamic>> artistResults,
  }) {
    final anchorTracks = _deriveSearchAnchorTracks(trackResults, limit: 2);
    final anchorArtistNames = _deriveArtistHintsFromTracks(anchorTracks);
    final normalizedAnchorArtists = anchorArtistNames
        .map(_normalizeSearchValue)
        .where((value) => value.isNotEmpty)
        .toSet();

    for (final artist in artistResults) {
      final normalizedName = _normalizeSearchValue(artist['name']?.toString());
      if (normalizedAnchorArtists.contains(normalizedName)) {
        return artist;
      }
    }

    final normalizedQuery = _normalizeSearchValue(query);
    if (artistResults.isNotEmpty) {
      final firstArtist = artistResults.first;
      final normalizedArtistName =
          _normalizeSearchValue(firstArtist['name']?.toString());
      final queryTokens = normalizedQuery
          .split(RegExp(r'\s+'))
          .where((token) => token.isNotEmpty)
          .toList(growable: false);
      final looksLikeLiteralEcho = normalizedQuery.isNotEmpty &&
          (normalizedArtistName == normalizedQuery ||
              (queryTokens.length >= 2 &&
                  queryTokens.every(normalizedArtistName.contains)));
      if (!looksLikeLiteralEcho) {
        return firstArtist;
      }
    }

    return _buildAnchorFeaturedArtist(anchorTracks);
  }

  void _captureSearchRecommendationContext({
    required String query,
    required Iterable<dynamic> trackResults,
    required Iterable<Map<String, dynamic>> artistResults,
  }) {
    _lastSearchRecommendationQuery = query.trim();
    _lastSearchRecommendationAnchorTracks =
        _deriveSearchAnchorTracks(trackResults);
    _lastSearchRecommendationArtistHints = _deriveSearchArtistHints(
      trackResults: _lastSearchRecommendationAnchorTracks,
      artistResults: artistResults,
    );
    _refreshRecommendationsOnSearchExit =
        _lastSearchRecommendationQuery.isNotEmpty ||
            _lastSearchRecommendationArtistHints.isNotEmpty;
  }

  void _clearSearchRecommendationContext() {
    _lastSearchRecommendationQuery = '';
    _lastSearchRecommendationArtistHints = const <String>[];
    _lastSearchRecommendationAnchorTracks = const <Map<String, dynamic>>[];
    _refreshRecommendationsOnSearchExit = false;
  }

  Future<void> _refreshHomeFromSearchContext() async {
    final query = _lastSearchRecommendationQuery.trim();
    final artistHints = List<String>.from(_lastSearchRecommendationArtistHints);
    _clearSearchRecommendationContext();
    final seed = await HistoryManager.getRecommendationSeed();
    if (!mounted) return;
    await ref.read(recommendationProvider.notifier).loadRecommendations(
          seed,
          true,
          artistHints,
          const <String>[],
          query.isEmpty ? const <String>[] : <String>[query],
        );
  }

  Future<void> _performSearch(WidgetRef ref, [String? query]) async {
    final q = query ?? _urlController.text.trim();
    if (q.isEmpty) {
      ref.read(searchPageProvider.notifier).clear();
      setState(() => _isSearching = false);
      return;
    }
    _urlController.text = q;
    _searchFocusNode.unfocus();
    setState(() => _isSearching = true);
    ref.read(suggestProvider.notifier).clear();
    await ref.read(searchPageProvider.notifier).search(q);
    final searchPage = ref.read(searchPageProvider);
    if (searchPage.requestState == 'complete' && searchPage.hasResults) {
      _captureSearchRecommendationContext(
        query: q,
        trackResults: searchPage.tracks,
        artistResults: searchPage.artists,
      );
    } else {
      _clearSearchRecommendationContext();
    }
  }

  void _clearSearch() {
    _isClearingSearch = true;
    _urlController.clear();
    _isClearingSearch = false;
    _searchFocusNode.unfocus();
    ref.read(suggestProvider.notifier).clear();
    ref.read(searchPageProvider.notifier).clear();
    setState(() => _isSearching = false);
    if (_refreshRecommendationsOnSearchExit) {
      _refreshRecommendationsOnSearchExit = false;
      unawaited(_refreshHomeFromSearchContext());
    }
  }

  Future<void> _refreshContent() async {
    if (_isSearching) {
      await _performSearch(ref);
      return;
    }
    await ref
        .read(recommendationProvider.notifier)
        .refreshFromSignals(forceRefresh: true);
    await Future.wait([
      ref.read(lastPlayedProvider.notifier).loadTracks(forceRefresh: true),
      ref
          .read(frequentlyPlayedProvider.notifier)
          .loadTracks(forceRefresh: true),
    ]);
  }

  Future<void> _deleteLastPlayedTrack(Map<String, dynamic> track) async {
    final videoId = extractTrackId(track);
    if (videoId == null || videoId.isEmpty) {
      return;
    }
    if (mounted) {
      setState(() {
        _pendingLastPlayedRemovals.add(videoId);
      });
    }
    await Future<void>.delayed(const Duration(milliseconds: 220));
    if (!mounted) return;
    setState(() {
      _hiddenLastPlayedTrackIds.add(videoId);
    });
    try {
      await HistoryManager.removeHistoryTrack(
        videoId,
        rawTrack: track,
      );
      if (!mounted) return;
      await Future.wait([
        ref.read(lastPlayedProvider.notifier).loadTracks(forceRefresh: true),
        ref
            .read(frequentlyPlayedProvider.notifier)
            .loadTracks(forceRefresh: true),
      ]);
      if (!mounted) return;
      await ref
          .read(recommendationProvider.notifier)
          .refreshFromSignals(forceRefresh: true);
      if (!mounted) return;
      setState(() {
        _pendingLastPlayedRemovals.remove(videoId);
        _hiddenLastPlayedTrackIds.remove(videoId);
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _pendingLastPlayedRemovals.remove(videoId);
        _hiddenLastPlayedTrackIds.remove(videoId);
      });
      rethrow;
    }
  }

  bool handleSystemBack() {
    if (hasActiveSearch) {
      _clearSearch();
      return true;
    }
    return false;
  }

  bool get hasActiveSearch =>
      _isSearching || _urlController.text.trim().isNotEmpty;

  String _buildHeroQuip() {
    final seed = DateTime.now().microsecondsSinceEpoch ^
        (_urlController.hashCode << 3) ^
        DateTime.now().second;
    final rng = math.Random(seed);
    return '${_quipOpeners[rng.nextInt(_quipOpeners.length)]}: '
        '${_quipMiddles[rng.nextInt(_quipMiddles.length)]}. '
        '${_quipClosers[rng.nextInt(_quipClosers.length)]}';
  }

  void _primeLikelyTracks(List<dynamic> tracks) {
    final idsToWarm = <String>[];
    for (final track in tracks.take(6)) {
      final id = (track['id'] ?? track['videoId'])?.toString();
      if (id == null || id.isEmpty) continue;
      if (_prewarmedTrackIds.add(id)) {
        idsToWarm.add(id);
      }
    }
    if (idsToWarm.isEmpty) return;

    final signature = idsToWarm.join('|');
    if (signature == _lastPrimeSignature) return;
    _lastPrimeSignature = signature;

    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStreams(idsToWarm));
  }

  Future<void> _openRecommendationRowDetail({
    required String rowId,
    required String title,
    required String itemType,
    required List<Map<String, dynamic>> items,
    required int nextOffset,
    required bool hasMore,
  }) async {
    if (itemType != 'track' || items.isEmpty) {
      return;
    }
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => RecommendationRowDetailScreen(
          initialRow: RecommendationFeedRowState(
            id: rowId,
            title: title,
            kind: rowId,
            itemType: itemType,
            items: items,
            nextOffset: nextOffset,
            hasMore: hasMore,
          ),
        ),
      ),
    );
  }

  Widget _buildRecommendationLaneHeader({
    required String title,
    VoidCallback? onOpenDetail,
  }) {
    return Row(
      children: [
        Expanded(
          child: Text(
            title,
            style: const TextStyle(
              fontSize: 18,
              fontWeight: FontWeight.bold,
              color: Colors.white,
            ),
          ),
        ),
        if (onOpenDetail != null)
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(999),
              onTap: onOpenDetail,
              child: Container(
                width: 34,
                height: 34,
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.05),
                  shape: BoxShape.circle,
                  border: Border.all(
                    color: Colors.white.withValues(alpha: 0.08),
                  ),
                ),
                child: const Icon(
                  Icons.arrow_forward_ios_rounded,
                  color: Colors.white70,
                  size: 15,
                ),
              ),
            ),
          ),
      ],
    );
  }

  Widget _buildRecommendationLaneSkeleton({int count = 3}) {
    return SizedBox(
      height: 244,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: count,
        separatorBuilder: (_, __) => const SizedBox(width: 14),
        itemBuilder: (context, index) => Container(
          width: 182,
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.035),
            borderRadius: BorderRadius.circular(_radiusLarge),
            border: Border.all(color: Colors.white.withValues(alpha: 0.05)),
          ),
        ),
      ),
    );
  }

  Widget _buildRecommendationLane({
    required String rowId,
    required String title,
    required String itemType,
    required List<Map<String, dynamic>> items,
    int nextOffset = 0,
    bool isLoading = false,
    bool isPaginating = false,
    bool hasMore = false,
  }) {
    if (isLoading && items.isEmpty) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: const TextStyle(
              fontSize: 18,
              fontWeight: FontWeight.bold,
              color: Colors.white,
            ),
          ),
          const SizedBox(height: 16),
          _buildRecommendationLaneSkeleton(),
        ],
      );
    }

    if (items.isEmpty) return const SizedBox.shrink();

    if (itemType == 'album') {
      return _buildAlbumRecommendationLane(
        rowId: rowId,
        title: title,
        items: items,
        nextOffset: nextOffset,
        isPaginating: isPaginating,
        hasMore: hasMore,
      );
    }

    if (itemType == 'artist') {
      return _buildArtistRow(
        items,
        title: title,
        isLoading: isLoading,
      );
    }

    if (rowId == 'quiet_picks') {
      return _buildVerticalRecommendationLane(
        rowId: rowId,
        title: title,
        items: items,
        nextOffset: nextOffset,
        isPaginating: isPaginating,
        hasMore: hasMore,
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildRecommendationLaneHeader(
          title: title,
          onOpenDetail: itemType == 'track'
              ? () => _openRecommendationRowDetail(
                    rowId: rowId,
                    title: title,
                    itemType: itemType,
                    items: items,
                    nextOffset: nextOffset,
                    hasMore: hasMore,
                  )
              : null,
        ),
        const SizedBox(height: 16),
        SizedBox(
          height: 244,
          child: NotificationListener<ScrollNotification>(
            onNotification: (notification) {
              if (_isSearching ||
                  notification.metrics.axis != Axis.horizontal) {
                return false;
              }
              if (!hasMore ||
                  isPaginating ||
                  notification.metrics.extentAfter > 280) {
                return false;
              }
              unawaited(
                ref.read(recommendationProvider.notifier).loadMoreRow(rowId),
              );
              return false;
            },
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              physics: const BouncingScrollPhysics(),
              itemCount: items.length + (hasMore || isPaginating ? 1 : 0),
              separatorBuilder: (_, __) => const SizedBox(width: 14),
              itemBuilder: (context, index) {
                if (index >= items.length) {
                  return Container(
                    width: 182,
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.035),
                      borderRadius: BorderRadius.circular(_radiusLarge),
                      border: Border.all(
                        color: Colors.white.withValues(alpha: 0.05),
                      ),
                    ),
                    child: Center(
                      child: isPaginating
                          ? const SizedBox(
                              width: 22,
                              height: 22,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: Colors.white70,
                              ),
                            )
                          : Icon(
                              Icons.chevron_right_rounded,
                              color: Colors.white.withValues(alpha: 0.5),
                              size: 28,
                            ),
                    ),
                  );
                }
                return _buildRecommendationCard(
                  items[index],
                  collectionTracks: items,
                  playlistId: 'row:$rowId',
                  playlistName: title,
                  showDeleteButton: rowId == 'last_played_local',
                  onDelete: rowId == 'last_played_local'
                      ? _deleteLastPlayedTrack
                      : null,
                  isDeleting: rowId == 'last_played_local' &&
                      _isLastPlayedDeleting(items[index]),
                );
              },
            ),
          ),
        ),
      ],
    );
  }

  Widget _buildVerticalRecommendationLane({
    required String rowId,
    required String title,
    required List<Map<String, dynamic>> items,
    required int nextOffset,
    required bool isPaginating,
    required bool hasMore,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildRecommendationLaneHeader(
          title: title,
          onOpenDetail: () => _openRecommendationRowDetail(
            rowId: rowId,
            title: title,
            itemType: 'track',
            items: items,
            nextOffset: nextOffset,
            hasMore: hasMore,
          ),
        ),
        const SizedBox(height: 16),
        _buildTrackList(
          items,
          false,
          '',
          playlistId: 'row:$rowId',
          playlistName: title,
          showTrailingLoader: isPaginating,
        ),
      ],
    );
  }

  Widget _buildAlbumRecommendationLane({
    required String rowId,
    required String title,
    required List<Map<String, dynamic>> items,
    required int nextOffset,
    required bool isPaginating,
    required bool hasMore,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildRecommendationLaneHeader(title: title),
        const SizedBox(height: 16),
        SizedBox(
          height: 258,
          child: NotificationListener<ScrollNotification>(
            onNotification: (notification) {
              if (_isSearching ||
                  notification.metrics.axis != Axis.horizontal) {
                return false;
              }
              if (!hasMore ||
                  isPaginating ||
                  notification.metrics.extentAfter > 280) {
                return false;
              }
              unawaited(
                ref.read(recommendationProvider.notifier).loadMoreRow(rowId),
              );
              return false;
            },
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              physics: const BouncingScrollPhysics(),
              itemCount: items.length + (hasMore || isPaginating ? 1 : 0),
              separatorBuilder: (_, __) => const SizedBox(width: 14),
              itemBuilder: (context, index) {
                if (index >= items.length) {
                  return Container(
                    width: 182,
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.035),
                      borderRadius: BorderRadius.circular(_radiusLarge),
                      border: Border.all(
                        color: Colors.white.withValues(alpha: 0.05),
                      ),
                    ),
                    child: Center(
                      child: isPaginating
                          ? const SizedBox(
                              width: 22,
                              height: 22,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: Colors.white70,
                              ),
                            )
                          : Icon(
                              Icons.chevron_right_rounded,
                              color: Colors.white.withValues(alpha: 0.5),
                              size: 28,
                            ),
                    ),
                  );
                }
                return _buildRecommendationAlbumCard(items[index]);
              },
            ),
          ),
        ),
      ],
    );
  }

  Widget _buildRecommendationCard(
    Map<String, dynamic> track, {
    required List<Map<String, dynamic>> collectionTracks,
    required String playlistId,
    required String playlistName,
    bool showDeleteButton = false,
    bool isDeleting = false,
    Future<void> Function(Map<String, dynamic> track)? onDelete,
  }) {
    final videoId = extractTrackId(track);
    final trackTitle = (() {
      final title = (track['title'] ?? track['name'] ?? track['song'])
              ?.toString()
              .trim() ??
          '';
      return title.isEmpty ? 'Unknown Track' : title;
    })();
    final trackArtist = (() {
      final artist = (track['channel'] ?? track['author'] ?? track['artist'])
              ?.toString()
              .trim() ??
          '';
      return artist;
    })();

    return AnimatedSlide(
      duration: const Duration(milliseconds: 220),
      curve: Curves.easeInOutCubic,
      offset: isDeleting ? const Offset(0.18, 0) : Offset.zero,
      child: AnimatedOpacity(
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeInOutCubic,
        opacity: isDeleting ? 0.0 : 1.0,
        child: AnimatedScale(
          duration: const Duration(milliseconds: 220),
          curve: Curves.easeInOutCubic,
          scale: isDeleting ? 0.92 : 1.0,
          child: Container(
            width: 182,
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.03),
              borderRadius: BorderRadius.circular(_radiusLarge),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.06),
                width: 1,
              ),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.18),
                  blurRadius: 16,
                  offset: const Offset(0, 8),
                ),
              ],
            ),
            child: Material(
              color: Colors.transparent,
              child: InkWell(
                borderRadius: BorderRadius.circular(_radiusLarge),
                onTapDown: (_) => _warmTrack(videoId),
                onTap: isDeleting
                    ? null
                    : () => _playTrackFromCollection(
                          tracks: collectionTracks,
                          track: track,
                          playlistId: playlistId,
                          playlistName: playlistName,
                        ),
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Expanded(
                        child: Stack(
                          children: [
                            Positioned.fill(
                              child: ClipRRect(
                                borderRadius: BorderRadius.circular(18),
                                child: AppArtwork(
                                  thumbnail: track['thumbnail'],
                                  videoId: videoId,
                                  width: double.infinity,
                                  height: double.infinity,
                                  radius: 18,
                                ),
                              ),
                            ),
                            if (showDeleteButton)
                              Positioned(
                                top: 8,
                                right: 8,
                                child: Material(
                                  color: Colors.transparent,
                                  child: InkWell(
                                    borderRadius: BorderRadius.circular(999),
                                    onTap: videoId == null ||
                                            onDelete == null ||
                                            isDeleting
                                        ? null
                                        : () => onDelete(
                                            Map<String, dynamic>.from(track)),
                                    child: Container(
                                      width: 32,
                                      height: 32,
                                      decoration: BoxDecoration(
                                        color: Colors.black
                                            .withValues(alpha: 0.48),
                                        shape: BoxShape.circle,
                                        border: Border.all(
                                          color: Colors.white
                                              .withValues(alpha: 0.08),
                                        ),
                                      ),
                                      child: const Icon(
                                        Icons.delete_outline_rounded,
                                        color: Colors.white,
                                        size: 18,
                                      ),
                                    ),
                                  ),
                                ),
                              ),
                          ],
                        ),
                      ),
                      const SizedBox(height: 12),
                      Text(
                        trackTitle,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Colors.white,
                          fontSize: 15,
                          fontWeight: FontWeight.w700,
                          height: 1.2,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        trackArtist,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.66),
                          fontSize: 12,
                        ),
                      ),
                      const SizedBox(height: 10),
                      Row(
                        children: [
                          InkWell(
                            onTap: isDeleting
                                ? null
                                : () => _playTrackFromCollection(
                                      tracks: collectionTracks,
                                      track: track,
                                      playlistId: playlistId,
                                      playlistName: playlistName,
                                    ),
                            borderRadius: BorderRadius.circular(999),
                            child: Container(
                              width: 38,
                              height: 38,
                              decoration: BoxDecoration(
                                color: Colors.white.withValues(alpha: 0.06),
                                shape: BoxShape.circle,
                              ),
                              child: const Icon(
                                Icons.play_arrow_rounded,
                                color: Colors.white,
                                size: 22,
                              ),
                            ),
                          ),
                          const Spacer(),
                          IconButton(
                            onPressed: videoId == null
                                ? null
                                : () => _openTrackDetails(track, videoId),
                            icon: Container(
                              padding: const EdgeInsets.all(8),
                              decoration: BoxDecoration(
                                color: Colors.white.withValues(alpha: 0.05),
                                shape: BoxShape.circle,
                              ),
                              child: const Icon(
                                Icons.info_outline_rounded,
                                color: Colors.white70,
                                size: 18,
                              ),
                            ),
                          ),
                          Consumer(
                            builder: (context, ref, child) {
                              final task = videoId == null
                                  ? null
                                  : ref.watch(downloadTaskProvider(videoId));
                              final isActive =
                                  task?.phase == DownloadPhase.active;
                              return IconButton(
                                icon: Container(
                                  padding: const EdgeInsets.all(8),
                                  decoration: BoxDecoration(
                                    color: Colors.white.withValues(alpha: 0.05),
                                    shape: BoxShape.circle,
                                  ),
                                  child: isActive
                                      ? SizedBox(
                                          width: 18,
                                          height: 18,
                                          child: CircularProgressIndicator(
                                            value: task!.progress > 0
                                                ? task.progress
                                                : null,
                                            strokeWidth: 2,
                                            color: _accentGrey,
                                          ),
                                        )
                                      : Icon(
                                          task?.phase == DownloadPhase.complete
                                              ? Icons.check_rounded
                                              : Icons.download_rounded,
                                          color: Colors.white70,
                                          size: 18,
                                        ),
                                ),
                                onPressed: videoId == null
                                    ? null
                                    : () {
                                        ref
                                            .read(
                                                downloadCenterProvider.notifier)
                                            .downloadTrack(track);
                                      },
                              );
                            },
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildRecommendationAlbumCard(Map<String, dynamic> album) {
    return SizedBox(
      width: 182,
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          borderRadius: BorderRadius.circular(_radiusLarge),
          onTap: () => _openAlbum(album),
          child: Container(
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.03),
              borderRadius: BorderRadius.circular(_radiusLarge),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.06),
                width: 1,
              ),
            ),
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                ClipRRect(
                  borderRadius: BorderRadius.circular(18),
                  child: AppArtwork(
                    thumbnail: album['thumbnail'],
                    width: double.infinity,
                    height: 138,
                    radius: 18,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  album['title']?.toString() ?? 'Unknown Album',
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 15,
                    fontWeight: FontWeight.w700,
                    height: 1.2,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  album['artist']?.toString() ?? 'Unknown Artist',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.66),
                    fontSize: 12,
                  ),
                ),
                const Spacer(),
                Row(
                  children: [
                    Icon(
                      Icons.album_rounded,
                      color: Colors.white.withValues(alpha: 0.72),
                      size: 16,
                    ),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        [
                          if ((album['year'] ?? '').toString().isNotEmpty)
                            album['year'].toString(),
                          if ((album['track_count'] ?? 0) > 0)
                            '${album['track_count']} tracks',
                        ].join(' • '),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.5),
                          fontSize: 11,
                        ),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildRecommendationFeedRows(
    RecommendationFeedState feedState, {
    required bool isLoading,
    required Set<String> hiddenKinds,
  }) {
    final errorMessage = feedState.errorMessage?.trim() ?? '';
    final allRows = feedState.rows.toList(growable: false);
    final filteredRows = feedState.rows
        .where((row) => !hiddenKinds.contains(row.kind))
        .toList(growable: false);
    final rows =
        filteredRows.isEmpty && allRows.isNotEmpty ? allRows : filteredRows;

    if (isLoading && rows.isEmpty) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _buildRecommendationLane(
            rowId: 'trending_for_you',
            title: 'Trending for you',
            itemType: 'track',
            items: const [],
            nextOffset: 0,
            isLoading: true,
          ),
          const SizedBox(height: 22),
          _buildRecommendationLane(
            rowId: 'quiet_picks',
            title: 'Quiet picks',
            itemType: 'track',
            items: const [],
            nextOffset: 0,
            isLoading: true,
          ),
        ],
      );
    }

    if (rows.isEmpty) {
      if (allRows.isNotEmpty) {
        return const SizedBox.shrink();
      }
      return Center(
        child: Text(
          errorMessage.isNotEmpty ? errorMessage : 'No recommendations yet.',
          style: TextStyle(
            color: errorMessage.isNotEmpty
                ? Colors.orangeAccent.withValues(alpha: 0.9)
                : Colors.white.withValues(alpha: 0.54),
          ),
        ),
      );
    }
    final lanes = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (var i = 0; i < rows.length; i++) ...[
          _buildRecommendationLane(
            rowId: rows[i].id,
            title: rows[i].title,
            itemType: rows[i].itemType,
            items: rows[i].items,
            nextOffset: rows[i].nextOffset,
            hasMore: rows[i].hasMore,
            isPaginating: ref
                .read(recommendationProvider.notifier)
                .isRowPaginating(rows[i].id),
          ),
          if (i != rows.length - 1) const SizedBox(height: 22),
        ],
      ],
    );
    if (errorMessage.isEmpty) {
      return lanes;
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: Colors.orangeAccent.withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(_radiusMedium),
            border:
                Border.all(color: Colors.orangeAccent.withValues(alpha: 0.25)),
          ),
          child: Text(
            errorMessage,
            style: TextStyle(
              color: Colors.orangeAccent.withValues(alpha: 0.95),
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
        const SizedBox(height: 14),
        lanes,
      ],
    );
  }

  void _warmTrack(String? videoId) {
    if (videoId == null || videoId.isEmpty) return;
    _prewarmedTrackIds.add(videoId);
    unawaited(ref.read(audioPlayerProvider.notifier).prewarmStream(videoId));
  }

  void _openTrackDetails(Map<String, dynamic> track, String? videoId) {
    if (videoId == null || videoId.isEmpty) return;
    _warmTrack(videoId);
    ref.read(trackDetailsProvider.notifier).fetchDetails(videoId);
    Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => TrackDetailsScreen(track: track)),
    );
  }

  Future<void> _playTrackFromCollection({
    required List<Map<String, dynamic>> tracks,
    required Map<String, dynamic> track,
    required String playlistId,
    required String playlistName,
  }) async {
    if (tracks.isEmpty) return;
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: playlistId,
          playlistName: playlistName,
          tracks: tracks,
          currentTrack: track,
        );
    if (!mounted) return;
    Navigator.of(context)
        .push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
  }

  Future<void> _playTrackAsDiscoveryMix(
    Map<String, dynamic> track, {
    String? sessionName,
  }) async {
    await ref.read(playbackQueueProvider.notifier).startDiscoverySession(
          track,
          sessionName: sessionName,
        );
    if (!mounted) return;
    Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  Future<void> _openAlbum(Map<String, dynamic> album) async {
    final albumId = album['id']?.toString();
    if (albumId == null || albumId.isEmpty) return;
    unawaited(ref.read(albumDetailsProvider.notifier).fetchAlbum(albumId));
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => AlbumDetailsScreen(
          albumId: albumId,
          fallbackAlbum: album,
        ),
      ),
    );
  }

  Future<void> _openArtist(Map<String, dynamic> artist) async {
    final artistId = artist['id']?.toString();
    if (artistId == null || artistId.isEmpty) return;
    unawaited(ref.read(artistDetailsProvider.notifier).fetchArtist(artistId));
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ArtistDetailsScreen(
          artistId: artistId,
          fallbackArtist: artist,
        ),
      ),
    );
    if (!mounted) return;
  }

  Future<void> _openAssistant() async {
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => AssistantScreen(
          onOpenAlbum: _openAlbum,
          onOpenPlayer: () {
            Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
            );
          },
        ),
      ),
    );
  }

  Widget _buildAlbumList(
    List<Map<String, dynamic>> albums,
    bool isLoading,
  ) {
    if (albums.isEmpty && isLoading) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 18),
          child: CircularProgressIndicator(color: _accentGrey),
        ),
      );
    }
    if (albums.isEmpty) {
      return const SizedBox.shrink();
    }

    return ListView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: albums.length,
      itemBuilder: (context, index) {
        final album = albums[index];
        return Container(
          margin: const EdgeInsets.only(bottom: 14),
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.025),
            borderRadius: BorderRadius.circular(_radiusLarge),
            border: Border.all(
              color: Colors.white.withValues(alpha: 0.06),
              width: 1,
            ),
          ),
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(_radiusLarge),
              onTap: () => _openAlbum(album),
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Row(
                  children: [
                    AppArtwork(
                      thumbnail: album['thumbnail'],
                      width: 78,
                      height: 78,
                      radius: 20,
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Container(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 10,
                              vertical: 5,
                            ),
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.06),
                              borderRadius: BorderRadius.circular(999),
                              border: Border.all(
                                color: Colors.white.withValues(alpha: 0.08),
                              ),
                            ),
                            child: Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Icon(
                                  Icons.album_outlined,
                                  size: 12,
                                  color: Colors.white.withValues(alpha: 0.72),
                                ),
                                const SizedBox(width: 6),
                                Text(
                                  'Album',
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.74),
                                    fontSize: 11,
                                    fontWeight: FontWeight.w700,
                                    letterSpacing: 0.4,
                                  ),
                                ),
                              ],
                            ),
                          ),
                          const SizedBox(height: 10),
                          Text(
                            album['title'] ?? 'Unknown Album',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          const SizedBox(height: 6),
                          Text(
                            album['artist'] ?? 'Unknown Artist',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.62),
                              fontSize: 13,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            [
                              if ((album['year'] ?? '').toString().isNotEmpty)
                                album['year'].toString(),
                              if ((album['track_count'] ?? 0) > 0)
                                '${album['track_count']} tracks',
                            ].join(' • '),
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.42),
                              fontSize: 12,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                        ],
                      ),
                    ),
                    Icon(
                      Icons.chevron_right_rounded,
                      color: Colors.white.withValues(alpha: 0.52),
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  Widget _buildArtistRow(
    List<Map<String, dynamic>> artists, {
    required String title,
    bool showTitle = true,
    bool isLoading = false,
  }) {
    if (artists.isEmpty && !isLoading) {
      return const SizedBox.shrink();
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (showTitle) ...[
          Text(
            title,
            style: const TextStyle(
              fontSize: 18,
              fontWeight: FontWeight.bold,
              color: Colors.white,
            ),
          ),
          const SizedBox(height: 14),
        ],
        SizedBox(
          height: 112,
          child: isLoading && artists.isEmpty
              ? ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: 5,
                  separatorBuilder: (_, __) => const SizedBox(width: 14),
                  itemBuilder: (_, __) => Column(
                    children: [
                      Container(
                        width: 68,
                        height: 68,
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.06),
                          shape: BoxShape.circle,
                        ),
                      ),
                      const SizedBox(height: 10),
                      Container(
                        width: 58,
                        height: 10,
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.06),
                          borderRadius: BorderRadius.circular(999),
                        ),
                      ),
                    ],
                  ),
                )
              : ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: artists.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 14),
                  itemBuilder: (context, index) {
                    final artist = artists[index];
                    return SizedBox(
                      width: 82,
                      child: Material(
                        color: Colors.transparent,
                        child: InkWell(
                          borderRadius: BorderRadius.circular(22),
                          onTap: () => _openArtist(artist),
                          child: Column(
                            children: [
                              AppArtwork(
                                thumbnail: artist['thumbnail'],
                                width: 68,
                                height: 68,
                                radius: 999,
                              ),
                              const SizedBox(height: 10),
                              Text(
                                artist['name']?.toString() ?? 'Artist',
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                textAlign: TextAlign.center,
                                style: TextStyle(
                                  color: Colors.white.withValues(alpha: 0.88),
                                  fontSize: 12,
                                  fontWeight: FontWeight.w600,
                                  height: 1.2,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    );
                  },
                ),
        ),
      ],
    );
  }

  Widget _buildFeaturedArtistCard(Map<String, dynamic> artist) {
    final description = (artist['description'] ?? '').toString().trim();
    final artistId = artist['id']?.toString().trim() ?? '';
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.03),
        borderRadius: BorderRadius.circular(_radiusLarge),
        border: Border.all(
          color: Colors.white.withValues(alpha: 0.08),
        ),
      ),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          borderRadius: BorderRadius.circular(_radiusLarge),
          onTap: artistId.isEmpty ? null : () => _openArtist(artist),
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Row(
              children: [
                AppArtwork(
                  thumbnail: artist['thumbnail'],
                  width: 88,
                  height: 88,
                  radius: 999,
                ),
                const SizedBox(width: 16),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 10,
                          vertical: 5,
                        ),
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.06),
                          borderRadius: BorderRadius.circular(999),
                          border: Border.all(
                            color: Colors.white.withValues(alpha: 0.08),
                          ),
                        ),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              Icons.mic_external_on_rounded,
                              size: 12,
                              color: Colors.white.withValues(alpha: 0.72),
                            ),
                            const SizedBox(width: 6),
                            Text(
                              'Artist',
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.74),
                                fontSize: 11,
                                fontWeight: FontWeight.w700,
                                letterSpacing: 0.4,
                              ),
                            ),
                          ],
                        ),
                      ),
                      const SizedBox(height: 10),
                      Text(
                        artist['name']?.toString() ?? 'Unknown Artist',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Colors.white,
                          fontSize: 18,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      if (description.isNotEmpty) ...[
                        const SizedBox(height: 6),
                        Text(
                          description,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.56),
                            fontSize: 12,
                            height: 1.35,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                Icon(
                  Icons.chevron_right_rounded,
                  color: Colors.white.withValues(alpha: 0.48),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  List<Map<String, dynamic>> _deriveAlbumsFromTracks(List<dynamic> tracks) {
    final seen = <String>{};
    final albums = <Map<String, dynamic>>[];

    for (final rawTrack in tracks) {
      if (rawTrack is! Map) continue;

      final albumTitle =
          (rawTrack['album_title'] ?? rawTrack['album'])?.toString().trim();
      if (albumTitle == null || albumTitle.isEmpty) continue;

      final albumId = rawTrack['album_id']?.toString().trim();
      final artist =
          (rawTrack['channel'] ?? rawTrack['author'] ?? '').toString().trim();
      final key = albumId != null && albumId.isNotEmpty
          ? 'id:$albumId'
          : 'title:${albumTitle.toLowerCase()}|artist:${artist.toLowerCase()}';
      if (!seen.add(key)) continue;

      albums.add({
        'id': albumId,
        'title': albumTitle,
        'artist': artist,
        'thumbnail': rawTrack['thumbnail'],
        'year': '',
        'track_count': 0,
      });
    }

    return albums;
  }

  Widget _buildTrackList(
    List<dynamic> tracks,
    bool isLoading,
    String emptyMessage, {
    String? playlistId,
    String? playlistName,
    bool playAsDiscoveryMix = false,
    bool showTrailingLoader = false,
  }) {
    if (isLoading && tracks.isEmpty) {
      return const TrackListSkeleton(count: 5);
    }
    if (tracks.isEmpty) {
      return Center(
        child:
            Text(emptyMessage, style: const TextStyle(color: Colors.white54)),
      );
    }
    final normalizedTracks =
        tracks.map((track) => normalizeTrack(track)).toList(growable: false);
    final loadingTileCount = showTrailingLoader ? 3 : 0;
    return ListView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: tracks.length + loadingTileCount,
      itemBuilder: (context, index) {
        if (index >= tracks.length) {
          return const TrackListSkeleton(count: 1);
        }
        final t = tracks[index];
        final videoId = (t['id'] ?? t['videoId'])?.toString();
        return Container(
          margin: const EdgeInsets.only(bottom: 14),
          decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.025),
              borderRadius: BorderRadius.circular(_radiusLarge),
              border: Border.all(
                  color: Colors.white.withValues(alpha: 0.06), width: 1),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.2),
                  blurRadius: 18,
                  offset: const Offset(0, 10),
                )
              ]),
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(16),
              onTapDown: (_) => _warmTrack(videoId),
              onTap: () {
                final normalizedTrack = Map<String, dynamic>.from(t as Map);
                if (playAsDiscoveryMix) {
                  _playTrackAsDiscoveryMix(
                    normalizedTrack,
                    sessionName: playlistName ?? 'Discovery mix',
                  );
                  return;
                }
                _playTrackFromCollection(
                  tracks: normalizedTracks,
                  track: normalizedTrack,
                  playlistId: playlistId ??
                      'list:${emptyMessage.hashCode}:${tracks.length}',
                  playlistName: playlistName ?? 'Track list',
                );
              },
              child: Padding(
                padding: const EdgeInsets.all(12.0),
                child: Row(
                  children: [
                    AppArtwork(
                      thumbnail: t['thumbnail'],
                      videoId: videoId,
                      width: 74,
                      height: 74,
                      radius: 18,
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(t['title'] ?? 'Unknown',
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                  color: Colors.white,
                                  fontSize: 16,
                                  fontWeight: FontWeight.w700)),
                          const SizedBox(height: 4),
                          Text(t['channel'] ?? '',
                              maxLines: 1,
                              style: TextStyle(
                                  color: Colors.white.withValues(alpha: 0.6),
                                  fontSize: 13)),
                        ],
                      ),
                    ),
                    IconButton(
                      onPressed: videoId == null
                          ? null
                          : () => _openTrackDetails(
                                Map<String, dynamic>.from(t as Map),
                                videoId,
                              ),
                      icon: Container(
                        padding: const EdgeInsets.all(8),
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.05),
                          shape: BoxShape.circle,
                        ),
                        child: const Icon(
                          Icons.info_outline_rounded,
                          color: Colors.white70,
                          size: 20,
                        ),
                      ),
                    ),
                    Consumer(
                      builder: (context, ref, child) {
                        final task = videoId == null
                            ? null
                            : ref.watch(downloadTaskProvider(videoId));
                        final isActive = task?.phase == DownloadPhase.active;
                        return IconButton(
                          icon: Container(
                            padding: const EdgeInsets.all(8),
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.05),
                              shape: BoxShape.circle,
                            ),
                            child: isActive
                                ? SizedBox(
                                    width: 20,
                                    height: 20,
                                    child: CircularProgressIndicator(
                                      value: task!.progress > 0
                                          ? task.progress
                                          : null,
                                      strokeWidth: 2,
                                      color: _accentGrey,
                                    ),
                                  )
                                : Icon(
                                    task?.phase == DownloadPhase.complete
                                        ? Icons.check_rounded
                                        : Icons.download_rounded,
                                    color: Colors.white70,
                                    size: 20,
                                  ),
                          ),
                          onPressed: videoId == null
                              ? null
                              : () {
                                  ref
                                      .read(downloadCenterProvider.notifier)
                                      .downloadTrack(t);
                                },
                        );
                      },
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    final searchPage = ref.watch(searchPageProvider);
    final isSearchLoading = ref.watch(searchPageProvider.notifier).isLoading;
    final fallbackAlbums = _isSearching
        ? _deriveAlbumsFromTracks(searchPage.tracks)
        : const <Map<String, dynamic>>[];
    final displayAlbums =
        searchPage.albums.isNotEmpty ? searchPage.albums : fallbackAlbums;
    final featuredArtist = _isSearching
        ? _resolveFeaturedSearchArtist(
            query: _urlController.text.trim(),
            trackResults: searchPage.tracks,
            artistResults: searchPage.artists,
          )
        : null;
    final similarArtists = _isSearching
        ? searchPage.similarArtists
        : const <Map<String, dynamic>>[];
    final featuredAlbum =
        _isSearching && displayAlbums.isNotEmpty ? displayAlbums.first : null;
    final extraAlbums = featuredAlbum == null
        ? displayAlbums
        : displayAlbums.skip(1).toList(growable: false);

    final recState = ref.watch(recommendationProvider);
    final isRecLoading = ref.watch(recommendationProvider.notifier).isLoading;
    final isRecPaginating =
        ref.watch(recommendationProvider.notifier).isPaginating;
    final lastPlayedTracks = _visibleLastPlayedTracks(
      ref.watch(lastPlayedProvider),
    );
    final isLastPlayedLoading =
        ref.watch(lastPlayedProvider.notifier).isLoading;
    final frequentTracks = ref.watch(frequentlyPlayedProvider);
    final isFrequentlyLoading =
        ref.watch(frequentlyPlayedProvider.notifier).isLoading;
    final suggestState = ref.watch(suggestProvider);
    final showSearchSuggestions = suggestState.isNotEmpty && !_isSearching;
    final visibleTracks =
        _isSearching ? searchPage.tracks : recState.visibleTracks;

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _primeLikelyTracks(visibleTracks);
    });
    _ensureRecommendationPageCanPaginate(
      recState,
      isRecLoading,
      isRecPaginating,
    );
    return SafeArea(
      child: RefreshIndicator(
        color: _accentGrey,
        backgroundColor: _surfaceGreyAlt,
        onRefresh: _refreshContent,
        child: NotificationListener<ScrollNotification>(
          onNotification: _handleHomeScrollNotification,
          child: SingleChildScrollView(
            controller: _homeScrollController,
            physics: const BouncingScrollPhysics(
              parent: AlwaysScrollableScrollPhysics(),
            ),
            padding: const EdgeInsets.all(16.0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Semantics(
                  label: _heroQuip,
                  child: TextField(
                    controller: _urlController,
                    focusNode: _searchFocusNode,
                    textInputAction: TextInputAction.search,
                    style: const TextStyle(color: Colors.white),
                    decoration: InputDecoration(
                      hintText: '',
                      filled: true,
                      fillColor: _surfaceGrey,
                      isDense: true,
                      contentPadding: const EdgeInsets.symmetric(vertical: 10),
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(_radiusLarge),
                        borderSide: BorderSide.none,
                      ),
                      prefixIcon: _isSearching || _urlController.text.isNotEmpty
                          ? IconButton(
                              icon: const Icon(Icons.arrow_back,
                                  color: Colors.white),
                              onPressed: _clearSearch,
                            )
                          : const Icon(Icons.search, color: Colors.white54),
                      prefixIconConstraints: const BoxConstraints(
                        minWidth: 40,
                        minHeight: 40,
                      ),
                      suffixIcon: _urlController.text.trim().isNotEmpty
                          ? IconButton(
                              icon: const Icon(
                                Icons.close_rounded,
                                color: Colors.white70,
                              ),
                              onPressed: _clearSearch,
                            )
                          : null,
                    ),
                    onSubmitted: (_) => _performSearch(ref),
                  ),
                ),

                if (showSearchSuggestions)
                  Container(
                    margin: const EdgeInsets.only(top: 8),
                    decoration: BoxDecoration(
                      color: Colors.grey[850],
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: ListView.builder(
                      shrinkWrap: true,
                      physics: const NeverScrollableScrollPhysics(),
                      itemCount: suggestState.length,
                      itemBuilder: (context, i) {
                        return ListTile(
                          leading:
                              const Icon(Icons.search, color: Colors.white54),
                          title: Text(
                            suggestState[i],
                            style: const TextStyle(color: Colors.white),
                          ),
                          onTap: () => _performSearch(ref, suggestState[i]),
                        );
                      },
                    ),
                  ),

                if (!_isSearching && !showSearchSuggestions) ...[
                  const SizedBox(height: 8),
                  Material(
                    color: Colors.transparent,
                    child: InkWell(
                      borderRadius: BorderRadius.circular(_radiusLarge),
                      onTap: _openAssistant,
                      child: Container(
                        width: double.infinity,
                        padding: const EdgeInsets.all(14),
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.03),
                          borderRadius: BorderRadius.circular(_radiusLarge),
                          border: Border.all(
                            color: Colors.white.withValues(alpha: 0.08),
                          ),
                        ),
                        child: Row(
                          children: [
                            Container(
                              width: 42,
                              height: 42,
                              decoration: BoxDecoration(
                                color: Colors.white.withValues(alpha: 0.06),
                                borderRadius:
                                    BorderRadius.circular(_radiusMedium),
                              ),
                              child: const Icon(
                                Icons.auto_awesome_rounded,
                                color: Colors.white,
                              ),
                            ),
                            const SizedBox(width: 12),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  const Text(
                                    'Ask EBB',
                                    style: TextStyle(
                                      color: Colors.white,
                                      fontSize: 15,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                  const SizedBox(height: 4),
                                  Text(
                                    'Describe a mood, artist, era, or playlist idea and get playable picks back.',
                                    style: TextStyle(
                                      color:
                                          Colors.white.withValues(alpha: 0.68),
                                      fontSize: 12,
                                      height: 1.4,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                            Icon(
                              Icons.chevron_right_rounded,
                              color: Colors.white.withValues(alpha: 0.6),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ],

                SizedBox(height: showSearchSuggestions ? 12 : 20),

                // Content Area (Search vs Recommendations)
                if (_isSearching) ...[
                  const Text('Search Results',
                      style: TextStyle(
                          fontSize: 18,
                          fontWeight: FontWeight.bold,
                          color: Colors.white)),
                  const SizedBox(height: 16),
                  if (featuredArtist != null) ...[
                    _buildFeaturedArtistCard(featuredArtist),
                  ] else if (isSearchLoading) ...[
                    _buildArtistRow(
                      const [],
                      title: 'Artist match',
                      isLoading: true,
                    ),
                    const SizedBox(height: 10),
                  ],
                  if (similarArtists.isNotEmpty || isSearchLoading) ...[
                    _buildArtistRow(
                      similarArtists,
                      title: 'Similar artists',
                      isLoading: isSearchLoading,
                    ),
                    const SizedBox(height: 8),
                  ],
                  if (featuredAlbum != null) ...[
                    _buildAlbumList([featuredAlbum], false),
                    const SizedBox(height: 10),
                  ],
                  _buildTrackList(
                    searchPage.tracks,
                    isSearchLoading,
                    searchPage.errorMessage?.trim().isNotEmpty == true
                        ? searchPage.errorMessage!.trim()
                        : "No results found.",
                    playlistId:
                        'search:${_urlController.text.trim().toLowerCase()}',
                    playlistName: _urlController.text.trim().isEmpty
                        ? 'Search results'
                        : 'Search: ${_urlController.text.trim()}',
                    playAsDiscoveryMix: true,
                  ),
                  if (extraAlbums.isNotEmpty || isSearchLoading) ...[
                    const SizedBox(height: 24),
                    const Text(
                      'More albums',
                      style: TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.bold,
                        color: Colors.white,
                      ),
                    ),
                    const SizedBox(height: 16),
                    _buildAlbumList(extraAlbums, isSearchLoading),
                  ],
                ] else ...[
                  if (lastPlayedTracks.isNotEmpty) ...[
                    _buildRecommendationLane(
                      rowId: 'last_played_local',
                      title: 'Last played',
                      itemType: 'track',
                      items: lastPlayedTracks,
                      nextOffset: lastPlayedTracks.length,
                      isLoading: isLastPlayedLoading,
                    ),
                    const SizedBox(height: 22),
                  ],
                  if (frequentTracks.isNotEmpty) ...[
                    _buildRecommendationLane(
                      rowId: 'frequently_listened_local',
                      title: 'Frequently listened',
                      itemType: 'track',
                      items: frequentTracks,
                      nextOffset: frequentTracks.length,
                      isLoading: isFrequentlyLoading,
                    ),
                    const SizedBox(height: 22),
                  ],
                  AnimatedSwitcher(
                    duration: const Duration(milliseconds: 220),
                    switchInCurve: Curves.easeOutCubic,
                    switchOutCurve: Curves.easeInCubic,
                    child: _buildRecommendationFeedRows(
                      recState,
                      isLoading: isRecLoading,
                      hiddenKinds: (frequentTracks.isNotEmpty &&
                              recState.rows.any(
                                (row) =>
                                    row.kind != 'frequently_listened' &&
                                    row.items.isNotEmpty,
                              ))
                          ? const {'frequently_listened'}
                          : const {},
                    ),
                  ),
                ],
                const SizedBox(height: 28),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class RecommendationRowDetailScreen extends ConsumerStatefulWidget {
  final RecommendationFeedRowState initialRow;

  const RecommendationRowDetailScreen({
    super.key,
    required this.initialRow,
  });

  @override
  ConsumerState<RecommendationRowDetailScreen> createState() =>
      _RecommendationRowDetailScreenState();
}

class _RecommendationRowDetailScreenState
    extends ConsumerState<RecommendationRowDetailScreen> {
  final ScrollController _scrollController = ScrollController();

  @override
  void initState() {
    super.initState();
    _scrollController.addListener(_handleScroll);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _handleScroll();
    });
  }

  @override
  void dispose() {
    _scrollController
      ..removeListener(_handleScroll)
      ..dispose();
    super.dispose();
  }

  RecommendationFeedRowState _resolvedRow(RecommendationFeedState state) {
    for (final row in state.rows) {
      if (row.id == widget.initialRow.id) {
        return row;
      }
    }
    return widget.initialRow;
  }

  void _handleScroll() {
    if (!_scrollController.hasClients) return;
    final row = _resolvedRow(ref.read(recommendationProvider));
    if (!row.hasMore ||
        ref.read(recommendationProvider.notifier).isRowPaginating(row.id) ||
        _scrollController.position.extentAfter > 700) {
      return;
    }
    unawaited(ref.read(recommendationProvider.notifier).loadMoreRow(row.id));
  }

  Future<void> _playTrack(
    RecommendationFeedRowState row,
    Map<String, dynamic> track,
  ) async {
    if (row.items.isEmpty) return;
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: 'row_detail:${row.id}',
          playlistName: row.title,
          tracks: row.items,
          currentTrack: track,
        );
    if (!mounted) return;
    Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  void _openTrackDetails(Map<String, dynamic> track) {
    final videoId = extractTrackId(track);
    if (videoId == null || videoId.isEmpty) return;
    ref.read(trackDetailsProvider.notifier).fetchDetails(videoId);
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => TrackDetailsScreen(track: track),
      ),
    );
  }

  String _durationLabel(dynamic rawDuration) {
    final totalSeconds = int.tryParse('${rawDuration ?? ''}') ?? 0;
    if (totalSeconds <= 0) return '--:--';
    final minutes = totalSeconds ~/ 60;
    final seconds = totalSeconds % 60;
    return '$minutes:${seconds.toString().padLeft(2, '0')}';
  }

  Color _rowAccentColor(String seed) {
    final palette = <Color>[
      const Color(0xFFC5372B),
      const Color(0xFFB84722),
      const Color(0xFF6E3CC9),
      const Color(0xFF245E8C),
      const Color(0xFF1E7A66),
    ];
    final hash = seed.codeUnits.fold<int>(0, (sum, code) => sum + code);
    return palette[hash % palette.length];
  }

  @override
  Widget build(BuildContext context) {
    final recState = ref.watch(recommendationProvider);
    final row = _resolvedRow(recState);
    final isPaginating =
        ref.watch(recommendationProvider.notifier).isRowPaginating(row.id);
    if (row.hasMore && !isPaginating) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        _handleScroll();
      });
    }
    final heroTrack =
        row.items.isNotEmpty ? row.items.first : const <String, dynamic>{};
    final accent = _rowAccentColor(row.id);

    return Scaffold(
      backgroundColor: _voidBlack,
      appBar: AppBar(
        title: Text(row.title),
      ),
      body: CustomScrollView(
        controller: _scrollController,
        physics: const BouncingScrollPhysics(),
        slivers: [
          SliverToBoxAdapter(
            child: Container(
              margin: const EdgeInsets.fromLTRB(16, 14, 16, 0),
              padding: const EdgeInsets.all(18),
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [
                    accent.withValues(alpha: 0.95),
                    accent.withValues(alpha: 0.72),
                    _surfaceGrey,
                  ],
                ),
                borderRadius: BorderRadius.circular(28),
                border: Border.all(
                  color: Colors.white.withValues(alpha: 0.08),
                ),
                boxShadow: [
                  BoxShadow(
                    color: accent.withValues(alpha: 0.22),
                    blurRadius: 24,
                    offset: const Offset(0, 12),
                  ),
                ],
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Made for your listening right now',
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.78),
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.35,
                    ),
                  ),
                  const SizedBox(height: 16),
                  Row(
                    crossAxisAlignment: CrossAxisAlignment.end,
                    children: [
                      Container(
                        padding: const EdgeInsets.all(6),
                        decoration: BoxDecoration(
                          color: Colors.black.withValues(alpha: 0.14),
                          borderRadius: BorderRadius.circular(22),
                          border: Border.all(
                            color: Colors.white.withValues(alpha: 0.12),
                          ),
                        ),
                        child: AppArtwork(
                          thumbnail: heroTrack['thumbnail'],
                          videoId: extractTrackId(heroTrack),
                          width: 112,
                          height: 112,
                          radius: 18,
                        ),
                      ),
                      const SizedBox(width: 18),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              row.title,
                              style: const TextStyle(
                                color: Colors.white,
                                fontSize: 31,
                                fontWeight: FontWeight.w900,
                                height: 1.04,
                              ),
                            ),
                            const SizedBox(height: 10),
                            Text(
                              row.hasMore
                                  ? '${row.items.length}+ tracks in this lane'
                                  : '${row.items.length} tracks in this lane',
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.78),
                                fontSize: 13,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 18),
                  Row(
                    children: [
                      InkWell(
                        onTap: row.items.isEmpty
                            ? null
                            : () => _playTrack(row, row.items.first),
                        borderRadius: BorderRadius.circular(999),
                        child: Container(
                          width: 54,
                          height: 54,
                          decoration: const BoxDecoration(
                            color: Colors.white,
                            shape: BoxShape.circle,
                          ),
                          child: Icon(
                            Icons.play_arrow_rounded,
                            color: accent,
                            size: 32,
                          ),
                        ),
                      ),
                      const SizedBox(width: 14),
                      Expanded(
                        child: Text(
                          'A deeper pass through ${row.title.toLowerCase()}.',
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.8),
                            fontSize: 13,
                            height: 1.35,
                          ),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
          SliverPadding(
            padding: const EdgeInsets.fromLTRB(16, 18, 16, 22),
            sliver: SliverList(
              delegate: SliverChildBuilderDelegate(
                (context, index) {
                  if (index >= row.items.length) {
                    return Padding(
                      padding: const EdgeInsets.symmetric(vertical: 18),
                      child: Center(
                        child: isPaginating
                            ? const SizedBox(
                                width: 26,
                                height: 26,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2.2,
                                  color: Colors.white70,
                                ),
                              )
                            : Text(
                                'Scroll for more',
                                style: TextStyle(
                                  color: Colors.white.withValues(alpha: 0.54),
                                  fontSize: 12,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                      ),
                    );
                  }

                  final track = row.items[index];
                  final videoId = extractTrackId(track);
                  return Container(
                    margin: const EdgeInsets.only(bottom: 12),
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.03),
                      borderRadius: BorderRadius.circular(22),
                      border: Border.all(
                        color: Colors.white.withValues(alpha: 0.06),
                      ),
                    ),
                    child: Material(
                      color: Colors.transparent,
                      child: InkWell(
                        borderRadius: BorderRadius.circular(22),
                        onTap: () => _playTrack(row, track),
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Row(
                            children: [
                              SizedBox(
                                width: 26,
                                child: Text(
                                  '${index + 1}',
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.56),
                                    fontSize: 12,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                              ),
                              const SizedBox(width: 8),
                              AppArtwork(
                                thumbnail: track['thumbnail'],
                                videoId: videoId,
                                width: 62,
                                height: 62,
                                radius: 18,
                              ),
                              const SizedBox(width: 14),
                              Expanded(
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      track['title']?.toString() ??
                                          'Unknown Track',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                        color: Colors.white,
                                        fontSize: 15,
                                        fontWeight: FontWeight.w700,
                                      ),
                                    ),
                                    const SizedBox(height: 4),
                                    Text(
                                      (track['channel'] ??
                                                  track['author'] ??
                                                  track['artist'])
                                              ?.toString() ??
                                          'Unknown Artist',
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: TextStyle(
                                        color: Colors.white
                                            .withValues(alpha: 0.68),
                                        fontSize: 12,
                                      ),
                                    ),
                                    if ((track['album']
                                            ?.toString()
                                            .trim()
                                            .isNotEmpty ??
                                        false)) ...[
                                      const SizedBox(height: 4),
                                      Text(
                                        track['album']!.toString(),
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                        style: TextStyle(
                                          color: Colors.white
                                              .withValues(alpha: 0.44),
                                          fontSize: 11,
                                        ),
                                      ),
                                    ],
                                  ],
                                ),
                              ),
                              const SizedBox(width: 8),
                              Text(
                                _durationLabel(track['duration']),
                                style: TextStyle(
                                  color: Colors.white.withValues(alpha: 0.52),
                                  fontSize: 11,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                              const SizedBox(width: 4),
                              IconButton(
                                onPressed: videoId == null
                                    ? null
                                    : () => _openTrackDetails(track),
                                icon: Container(
                                  padding: const EdgeInsets.all(8),
                                  decoration: BoxDecoration(
                                    color: Colors.white.withValues(alpha: 0.05),
                                    shape: BoxShape.circle,
                                  ),
                                  child: const Icon(
                                    Icons.info_outline_rounded,
                                    color: Colors.white70,
                                    size: 18,
                                  ),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  );
                },
                childCount:
                    row.items.length + ((row.hasMore || isPaginating) ? 1 : 0),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
