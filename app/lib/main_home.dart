part of 'main.dart';

class HomeScreen extends ConsumerStatefulWidget {
  final ValueChanged<bool>? onSearchModeChanged;
  final bool searchOnly;

  const HomeScreen({
    super.key,
    this.onSearchModeChanged,
    this.searchOnly = false,
  });

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
  final Map<String, String> _selectedGenreTabs = <String, String>{};
  final Map<String, int> _genrePageIndexes = <String, int>{};
  List<String> _recentSearchHistory = const <String>[];
  bool _isRecentSearchHistoryLoading = false;
  String _lastPrimeSignature = '';
  late final String _heroQuip;

  @override
  void initState() {
    super.initState();
    _isSearching = widget.searchOnly;
    _heroQuip = _buildHeroQuip();
    _homeScrollController.addListener(_handleHomeScroll);
    unawaited(_loadRecentSearchHistory());
    _urlController.addListener(() {
      if (_isClearingSearch) {
        return;
      }
      final text = _urlController.text.trim();
      if (mounted) {
        setState(() {});
      }
      if (text.isNotEmpty) {
        _suggestDebounce?.cancel();
        _suggestDebounce = Timer(const Duration(milliseconds: 280), () {
          if (mounted) {
            ref.read(suggestProvider.notifier).fetchSuggestions(text);
          }
        });
      } else if (text.isEmpty) {
        _suggestDebounce?.cancel();
        ref.read(suggestProvider.notifier).clear();
        ref.read(searchPageProvider.notifier).clear();
        if (widget.searchOnly) {
          _setSearchMode(true);
        } else if (_isSearching) {
          _setSearchMode(false);
        }
      }
    });
    if (widget.searchOnly) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        _searchFocusNode.requestFocus();
      });
    }
  }

  void _setSearchMode(bool value) {
    final nextValue = widget.searchOnly ? true : value;
    if (_isSearching == nextValue) {
      return;
    }
    setState(() => _isSearching = nextValue);
    if (!widget.searchOnly) {
      widget.onSearchModeChanged?.call(nextValue);
    }
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
    if (position.extentAfter > 1200) return;
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
        rowStyle: '',
        meta: <String, dynamic>{},
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

  Future<void> _loadRecentSearchHistory() async {
    if (_isRecentSearchHistoryLoading) return;
    final cached = peekRecentCloudSearchQueries(limit: 8);
    if (cached.isNotEmpty && mounted) {
      setState(() => _recentSearchHistory = cached);
    }
    _isRecentSearchHistoryLoading = true;
    try {
      final recentQueries = await getRecentCloudSearchQueries(limit: 8);
      if (!mounted) return;
      setState(() {
        _recentSearchHistory = recentQueries;
      });
    } finally {
      _isRecentSearchHistoryLoading = false;
    }
  }

  Future<void> _refreshHomeFromSearchContext() async {
    final query = _lastSearchRecommendationQuery.trim();
    final artistHints = List<String>.from(_lastSearchRecommendationArtistHints);
    _clearSearchRecommendationContext();
    final notifier = ref.read(recommendationProvider.notifier);
    notifier.queueSessionIntent(
      artistHints: artistHints,
      sessionQueries: query.isEmpty ? const <String>[] : <String>[query],
    );
    if (!mounted) return;
    await notifier.applyQueuedSessionIntent();
  }

  Future<void> _performSearch(WidgetRef ref, [String? query]) async {
    final q = query ?? _urlController.text.trim();
    if (q.isEmpty) {
      ref.read(searchPageProvider.notifier).clear();
      _setSearchMode(widget.searchOnly);
      return;
    }
    _urlController.text = q;
    _searchFocusNode.unfocus();
    _setSearchMode(true);
    ref.read(suggestProvider.notifier).clear();
    await ref.read(searchPageProvider.notifier).search(q);
    unawaited(_loadRecentSearchHistory());
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

  void _clearSearch({bool refreshRecommendations = true}) {
    _isClearingSearch = true;
    _urlController.clear();
    _isClearingSearch = false;
    _searchFocusNode.unfocus();
    ref.read(suggestProvider.notifier).clear();
    ref.read(searchPageProvider.notifier).clear();
    _setSearchMode(widget.searchOnly);
    if (!widget.searchOnly &&
        refreshRecommendations &&
        _refreshRecommendationsOnSearchExit) {
      _refreshRecommendationsOnSearchExit = false;
      unawaited(_refreshHomeFromSearchContext());
    } else {
      _clearSearchRecommendationContext();
    }
    if (widget.searchOnly) {
      unawaited(_loadRecentSearchHistory());
    }
  }

  void focusSearch() {
    if (!mounted) return;
    if (_homeScrollController.hasClients) {
      _homeScrollController.animateTo(
        0,
        duration: const Duration(milliseconds: 240),
        curve: Curves.easeOutCubic,
      );
    }
    _setSearchMode(true);
    unawaited(_loadRecentSearchHistory());
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _searchFocusNode.requestFocus();
      _urlController.selection = TextSelection.fromPosition(
        TextPosition(offset: _urlController.text.length),
      );
    });
  }

  void showHomeFeed() {
    if (!mounted) return;
    if (widget.searchOnly) return;
    _refreshRecommendationsOnSearchExit = false;
    _clearSearch(refreshRecommendations: false);
    if (_homeScrollController.hasClients) {
      _homeScrollController.animateTo(
        0,
        duration: const Duration(milliseconds: 240),
        curve: Curves.easeOutCubic,
      );
    }
  }

  Future<void> _refreshContent() async {
    if (_isSearching || widget.searchOnly) {
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
    if (widget.searchOnly) {
      final hasSearchContent = _urlController.text.trim().isNotEmpty ||
          ref.read(searchPageProvider).hasResults ||
          ref.read(suggestProvider).isNotEmpty;
      if (hasSearchContent) {
        _clearSearch(refreshRecommendations: false);
        return true;
      }
      return false;
    }
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
    String rowStyle = '',
    Map<String, dynamic> meta = const <String, dynamic>{},
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
            rowStyle: rowStyle,
            meta: meta,
            items: items,
            nextOffset: nextOffset,
            hasMore: hasMore,
          ),
          playerScreenBuilder: (_) => const FullPlayerScreen(),
          trackDetailsScreenBuilder: (track) =>
              TrackDetailsScreen(track: track),
          onPrimeTrack: _warmTrack,
        ),
      ),
    );
  }

  Color _parseLaneColor(
    String? rawColor, {
    Color fallback = const Color(0xFF8A4F35),
  }) {
    final value = rawColor?.trim() ?? '';
    if (value.isEmpty) return fallback;
    final hex = value.replaceFirst('#', '');
    if (hex.length != 6) return fallback;
    final parsed = int.tryParse('FF$hex', radix: 16);
    if (parsed == null) return fallback;
    return Color(parsed);
  }

  List<Map<String, dynamic>> _mixTracks(Map<String, dynamic> mix) {
    final rawTracks = mix['tracks'] as List<dynamic>? ?? const [];
    return rawTracks
        .whereType<Map>()
        .map((track) => normalizeTrack(Map<String, dynamic>.from(track)))
        .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
        .toList(growable: false);
  }

  Future<void> _playFeaturedTrack(
    Map<String, dynamic> track, {
    required String sessionName,
  }) async {
    await _playTrackAsDiscoveryMix(track, sessionName: sessionName);
  }

  Future<void> _playMixCard(
    Map<String, dynamic> mix, {
    required String playlistId,
  }) async {
    final tracks = _mixTracks(mix);
    if (tracks.isEmpty) return;
    final mixTitle = mix['title']?.toString().trim() ?? '';
    await ref.read(playbackQueueProvider.notifier).startPlaylistSession(
          playlistId: playlistId,
          playlistName: mixTitle.isNotEmpty ? mixTitle : 'Mix for you',
          tracks: tracks,
          currentTrack: tracks.first,
        );
    if (!mounted) return;
    Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  Future<void> _openMixDetail(Map<String, dynamic> mix) async {
    final tracks = _mixTracks(mix);
    if (tracks.isEmpty) return;
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => PersonalMixDetailScreen(
          mix: Map<String, dynamic>.from(mix),
          tracks: tracks,
          playerScreenBuilder: (_) => const FullPlayerScreen(),
          trackDetailsScreenBuilder: (track) =>
              TrackDetailsScreen(track: track),
          onPrimeTrack: _warmTrack,
        ),
      ),
    );
  }

  Widget _buildFeaturedTrackLane({
    required String title,
    required List<Map<String, dynamic>> items,
    required Map<String, dynamic> meta,
  }) {
    final track = items.first;
    final accent = _parseLaneColor(
      meta['accent_color']?.toString(),
      fallback: const Color(0xFF9A4C2A),
    );
    final trackId = extractTrackId(track);
    final artistName = (track['channel'] ?? track['author'] ?? track['artist'])
            ?.toString()
            .trim() ??
        'Unknown artist';
    final metaEyebrow = meta['eyebrow']?.toString().trim() ?? '';

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          metaEyebrow.isNotEmpty ? metaEyebrow : "TODAY'S PICK",
          style: TextStyle(
            color: Colors.white.withValues(alpha: 0.68),
            fontSize: 12,
            fontWeight: FontWeight.w700,
            letterSpacing: 0.6,
          ),
        ),
        const SizedBox(height: 6),
        Text(
          title,
          style: const TextStyle(
            fontSize: 32,
            fontWeight: FontWeight.w900,
            color: Colors.white,
            height: 1.02,
          ),
        ),
        const SizedBox(height: 16),
        Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(26),
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [
                accent.withValues(alpha: 0.95),
                accent.withValues(alpha: 0.72),
                _surfaceGrey,
              ],
            ),
            border: Border.all(
              color: Colors.white.withValues(alpha: 0.1),
            ),
            boxShadow: [
              BoxShadow(
                color: accent.withValues(alpha: 0.18),
                blurRadius: 26,
                offset: const Offset(0, 14),
              ),
            ],
          ),
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(26),
              onTap: () => _playFeaturedTrack(track, sessionName: title),
              child: Padding(
                padding: const EdgeInsets.all(18),
                child: Row(
                  children: [
                    Stack(
                      alignment: Alignment.center,
                      children: [
                        Container(
                          width: 126,
                          height: 126,
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            color: Colors.black.withValues(alpha: 0.12),
                          ),
                        ),
                        Container(
                          padding: const EdgeInsets.all(6),
                          decoration: BoxDecoration(
                            color: Colors.black.withValues(alpha: 0.16),
                            borderRadius: BorderRadius.circular(24),
                            border: Border.all(
                              color: Colors.white.withValues(alpha: 0.1),
                            ),
                          ),
                          child: AppArtwork(
                            thumbnail: track['thumbnail'],
                            videoId: trackId,
                            width: 112,
                            height: 112,
                            radius: 22,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(width: 18),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            track['title']?.toString() ?? 'Unknown track',
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              fontSize: 24,
                              fontWeight: FontWeight.w800,
                              color: Colors.white,
                              height: 1.1,
                            ),
                          ),
                          const SizedBox(height: 8),
                          Text(
                            artistName,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.86),
                              fontSize: 16,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          const SizedBox(height: 16),
                          Row(
                            children: [
                              InkWell(
                                onTap: () => _playFeaturedTrack(track,
                                    sessionName: title),
                                borderRadius: BorderRadius.circular(999),
                                child: Container(
                                  width: 58,
                                  height: 58,
                                  decoration: const BoxDecoration(
                                    color: Colors.white,
                                    shape: BoxShape.circle,
                                  ),
                                  child: Icon(
                                    Icons.play_arrow_rounded,
                                    color: accent,
                                    size: 34,
                                  ),
                                ),
                              ),
                              const SizedBox(width: 12),
                              TrackMenuButton(
                                track: Map<String, dynamic>.from(track),
                                onOpenDetails: () =>
                                    _openTrackDetails(track, trackId),
                                onAddToPlaylist: _addTrackToPlaylistFromMenu,
                                onStartStation: _startTrackStationFromMenu,
                                buttonSize: 46,
                                iconSize: 20,
                              ),
                            ],
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }

  Widget _buildMixCardsLane({
    required String title,
    required List<Map<String, dynamic>> items,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildRecommendationLaneHeader(title: title),
        const SizedBox(height: 16),
        SizedBox(
          height: 284,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            physics: const BouncingScrollPhysics(),
            itemCount: items.length,
            separatorBuilder: (_, __) => const SizedBox(width: 14),
            itemBuilder: (context, index) {
              final mix = items[index];
              final accent = _parseLaneColor(
                mix['accent_color']?.toString(),
                fallback: index.isEven
                    ? const Color(0xFF6C7BFF)
                    : const Color(0xFF8D724B),
              );
              final tracks = _mixTracks(mix);
              final mixTitle = mix['title']?.toString().trim() ?? 'Mix';
              final isGenericMixTitle =
                  RegExp(r'^mix\s+\d+$', caseSensitive: false)
                      .hasMatch(mixTitle);
              return SizedBox(
                width: 210,
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    borderRadius: BorderRadius.circular(24),
                    onTap: () => _openMixDetail(mix),
                    child: Container(
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.035),
                        borderRadius: BorderRadius.circular(24),
                        border: Border.all(
                          color: Colors.white.withValues(alpha: 0.07),
                        ),
                      ),
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Stack(
                              children: [
                                ClipRRect(
                                  borderRadius: BorderRadius.circular(18),
                                  child: SizedBox(
                                    width: double.infinity,
                                    height: 138,
                                    child: AppArtwork(
                                      thumbnail: mix['thumbnail'],
                                      videoId: tracks.isNotEmpty
                                          ? extractTrackId(tracks.first)
                                          : null,
                                      width: double.infinity,
                                      height: 138,
                                      radius: 18,
                                    ),
                                  ),
                                ),
                                Positioned(
                                  left: 10,
                                  bottom: 10,
                                  child: Container(
                                    padding: const EdgeInsets.symmetric(
                                      horizontal: 10,
                                      vertical: 5,
                                    ),
                                    decoration: BoxDecoration(
                                      color: accent,
                                      borderRadius: BorderRadius.circular(10),
                                    ),
                                    child: Text(
                                      mix['badge']?.toString() ?? 'MIX',
                                      style: TextStyle(
                                        color: accent.computeLuminance() > 0.65
                                            ? Colors.black87
                                            : Colors.white,
                                        fontSize: 11,
                                        fontWeight: FontWeight.w900,
                                      ),
                                    ),
                                  ),
                                ),
                              ],
                            ),
                            SizedBox(height: isGenericMixTitle ? 10 : 12),
                            if (!isGenericMixTitle) ...[
                              Text(
                                mixTitle,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  color: Colors.white,
                                  fontSize: 18,
                                  fontWeight: FontWeight.w800,
                                ),
                              ),
                              const SizedBox(height: 6),
                            ],
                            Text(
                              mix['subtitle']?.toString() ??
                                  'Picked from your listening.',
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                color: Colors.white.withValues(alpha: 0.68),
                                fontSize: 13,
                                height: 1.25,
                              ),
                            ),
                            const Spacer(),
                            Row(
                              children: [
                                Expanded(
                                  child: Text(
                                    '${mix['track_count'] ?? tracks.length} tracks',
                                    style: TextStyle(
                                      color:
                                          Colors.white.withValues(alpha: 0.46),
                                      fontSize: 11,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                ),
                                InkWell(
                                  onTap: () => _playMixCard(
                                    mix,
                                    playlistId: 'mix:${mix['id'] ?? index}',
                                  ),
                                  borderRadius: BorderRadius.circular(999),
                                  child: Container(
                                    width: 34,
                                    height: 34,
                                    decoration: BoxDecoration(
                                      color:
                                          Colors.white.withValues(alpha: 0.08),
                                      shape: BoxShape.circle,
                                    ),
                                    child: const Icon(
                                      Icons.play_arrow_rounded,
                                      color: Colors.white,
                                      size: 20,
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
                ),
              );
            },
          ),
        ),
      ],
    );
  }

  Widget _buildGenreTabsLane({
    required String rowId,
    required String title,
    required List<Map<String, dynamic>> fallbackItems,
    required Map<String, dynamic> meta,
    required bool isUpdating,
  }) {
    final rawTabs = (meta['tabs'] as List<dynamic>? ?? const [])
        .whereType<Map>()
        .map((tab) => Map<String, dynamic>.from(tab))
        .toList(growable: false);
    if (rawTabs.isEmpty) {
      return const SizedBox.shrink();
    }
    final queuedTabId = _selectedGenreTabs[rowId]?.trim() ?? '';
    final activeTabId = meta['active_tab_id']?.toString().trim() ?? '';
    final initialTabId = queuedTabId.isNotEmpty
        ? queuedTabId
        : activeTabId.isNotEmpty
            ? activeTabId
            : rawTabs.first['id']?.toString().trim() ?? '';
    final activeTab = rawTabs.firstWhere(
      (tab) => (tab['id']?.toString().trim() ?? '') == initialTabId,
      orElse: () => rawTabs.first,
    );
    final activeTracksRaw =
        (activeTab['tracks'] as List<dynamic>? ?? fallbackItems)
            .whereType<Map>();
    final activeTracks = activeTracksRaw
        .map((track) => normalizeTrack(Map<String, dynamic>.from(track)))
        .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
        .toList(growable: false);
    final rawPages = (activeTab['pages'] as List<dynamic>? ?? const [])
        .whereType<List>()
        .map((page) => page.whereType<Map>())
        .toList(growable: false);
    final pageTracks = rawPages
        .map(
          (page) => page
              .map((track) => normalizeTrack(Map<String, dynamic>.from(track)))
              .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
              .toList(growable: false),
        )
        .where((page) => page.isNotEmpty)
        .toList(growable: false);
    final pages = pageTracks.isNotEmpty
        ? pageTracks
        : <List<Map<String, dynamic>>>[
            for (var index = 0; index < activeTracks.length; index += 4)
              activeTracks.sublist(
                index,
                (index + 4) > activeTracks.length
                    ? activeTracks.length
                    : (index + 4),
              ),
          ];
    if (pages.isEmpty) {
      return const SizedBox.shrink();
    }
    final pageKey = '$rowId:$initialTabId';
    final currentPageIndex = pages.isEmpty
        ? 0
        : (_genrePageIndexes[pageKey] ?? 0).clamp(0, pages.length - 1);
    final accent = _parseLaneColor(
      meta['accent_color']?.toString(),
      fallback: const Color(0xFF245E8C),
    );

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildRecommendationLaneHeader(title: title),
        const SizedBox(height: 14),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          physics: const BouncingScrollPhysics(),
          child: Row(
            children: [
              for (final tab in rawTabs) ...[
                GenreTabChip(
                  label: tab['label']?.toString() ?? 'Genre',
                  selected:
                      (tab['id']?.toString().trim() ?? '') == initialTabId,
                  onTap: () {
                    final nextTabId = tab['id']?.toString().trim() ?? '';
                    if (nextTabId.isEmpty) return;
                    setState(() {
                      _selectedGenreTabs[rowId] = nextTabId;
                      _genrePageIndexes['$rowId:$nextTabId'] = 0;
                    });
                    unawaited(
                      ref
                          .read(recommendationProvider.notifier)
                          .selectRowContext(
                            rowId,
                            rowContext: 'genre_tab:$nextTabId',
                          ),
                    );
                  },
                ),
                const SizedBox(width: 10),
              ],
            ],
          ),
        ),
        const SizedBox(height: 12),
        AnimatedOpacity(
          opacity: isUpdating ? 1 : 0,
          duration: const Duration(milliseconds: 180),
          child: Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: Row(
              children: [
                SizedBox(
                  width: 14,
                  height: 14,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: Colors.blueAccent.shade100,
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  'Refreshing this genre lane...',
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.62),
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ],
            ),
          ),
        ),
        Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(24),
            gradient: LinearGradient(
              begin: Alignment.topCenter,
              end: Alignment.bottomCenter,
              colors: [
                accent.withValues(alpha: 0.28),
                Colors.transparent,
              ],
            ),
          ),
          child: AnimatedSwitcher(
            duration: const Duration(milliseconds: 240),
            switchInCurve: Curves.easeOutCubic,
            switchOutCurve: Curves.easeInCubic,
            child: Column(
              key: ValueKey<String>(initialTabId),
              children: [
                SizedBox(
                  height: 345,
                  child: PageView.builder(
                    key: PageStorageKey<String>('genre-pages:$pageKey'),
                    physics: pages.length > 1
                        ? const BouncingScrollPhysics()
                        : const NeverScrollableScrollPhysics(),
                    onPageChanged: (pageIndex) {
                      if (!mounted) return;
                      setState(() {
                        _genrePageIndexes[pageKey] = pageIndex;
                      });
                    },
                    itemCount: pages.length,
                    itemBuilder: (context, pageIndex) {
                      final page = pages[pageIndex];
                      return AnimatedSlide(
                        duration: const Duration(milliseconds: 220),
                        curve: Curves.easeOutCubic,
                        offset: Offset(
                          pageIndex == currentPageIndex ? 0 : 0.02,
                          0,
                        ),
                        child: Column(
                          children: [
                            for (var index = 0; index < page.length; index++) ...[
                              GenreTrackTile(
                                track: page[index],
                                onPlay: () => _playTrackFromCollection(
                                  tracks: activeTracks,
                                  track: page[index],
                                  playlistId: 'genre:$rowId:$initialTabId',
                                  playlistName:
                                      '$title • ${activeTab['label']?.toString() ?? 'Genre'}',
                                ),
                                onOpenDetails: () => _openTrackDetails(
                                  page[index],
                                  extractTrackId(page[index]),
                                ),
                                onAddToPlaylist: _addTrackToPlaylistFromMenu,
                                onStartStation: _startTrackStationFromMenu,
                              ),
                              if (index != page.length - 1)
                                Divider(
                                  height: 1,
                                  color: Colors.white.withValues(alpha: 0.05),
                                ),
                            ],
                          ],
                        ),
                      );
                    },
                  ),
                ),
                if (pages.length > 1) ...[
                  const SizedBox(height: 12),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      for (var index = 0; index < pages.length; index++) ...[
                        AnimatedContainer(
                          duration: const Duration(milliseconds: 200),
                          curve: Curves.easeOutCubic,
                          width: index == currentPageIndex ? 18 : 7,
                          height: 7,
                          decoration: BoxDecoration(
                            color: index == currentPageIndex
                                ? Colors.white.withValues(alpha: 0.92)
                                : Colors.white.withValues(alpha: 0.22),
                            borderRadius: BorderRadius.circular(999),
                          ),
                        ),
                        if (index != pages.length - 1)
                          const SizedBox(width: 6),
                      ],
                    ],
                  ),
                ],
              ],
            ),
          ),
        ),
      ],
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
    String rowStyle = '',
    Map<String, dynamic> meta = const <String, dynamic>{},
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

    if (rowStyle == 'featured_track') {
      return _buildFeaturedTrackLane(
        title: title,
        items: items,
        meta: meta,
      );
    }

    if (rowStyle == 'mix_cards' || itemType == 'mix') {
      return _buildMixCardsLane(
        title: title,
        items: items,
      );
    }

    if (rowStyle == 'genre_tabs') {
      return _buildGenreTabsLane(
        rowId: rowId,
        title: title,
        fallbackItems: items,
        meta: meta,
        isUpdating: isPaginating,
      );
    }

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
                    rowStyle: rowStyle,
                    meta: meta,
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
          nearEndLead: 5,
          onNearEnd: (!hasMore || isPaginating)
              ? null
              : () => ref.read(recommendationProvider.notifier).loadMoreRow(rowId),
        ),
        if (hasMore || isPaginating)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Center(
              child: TextButton(
                onPressed: isPaginating
                    ? null
                    : () => ref.read(recommendationProvider.notifier).loadMoreRow(rowId),
                style: TextButton.styleFrom(
                  foregroundColor: Colors.white.withValues(alpha: 0.74),
                ),
                child: isPaginating
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('Load more'),
              ),
            ),
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
              color: const Color(0xFF1B1D20),
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
                                        color: const Color(0xFF23262A)
                                            .withValues(alpha: 0.92),
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
                          const Spacer(),
                          TrackMenuButton(
                            track: Map<String, dynamic>.from(track),
                            onOpenDetails: () =>
                                _openTrackDetails(track, videoId),
                            onAddToPlaylist: _addTrackToPlaylistFromMenu,
                            onStartStation: _startTrackStationFromMenu,
                            buttonSize: 38,
                            iconSize: 18,
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
                ? const Color(0xFF8B939C)
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
            rowStyle: rows[i].rowStyle,
            meta: rows[i].meta,
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
            color: const Color(0xFF8B939C).withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(_radiusMedium),
            border: Border.all(
              color: const Color(0xFF8B939C).withValues(alpha: 0.25),
            ),
          ),
          child: Text(
            errorMessage,
            style: const TextStyle(
              color: Color(0xFFB3BAC1),
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

  Future<void> _addTrackToPlaylistFromMenu(Map<String, dynamic> track) async {
    if (!mounted) return;
    showAddToPlaylistDialog(
      context: context,
      track: Map<String, dynamic>.from(track),
    );
  }

  Future<void> _startTrackStationFromMenu(Map<String, dynamic> track) async {
    await ref.read(playbackQueueProvider.notifier).startRadioSession(track);
    if (!mounted) return;
    Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
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
    VoidCallback? onNearEnd,
    int nearEndLead = 2,
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
        final nearEndIndex = max(0, tracks.length - max(1, nearEndLead));
        if (onNearEnd != null && index >= nearEndIndex) {
          WidgetsBinding.instance.addPostFrameCallback((_) {
            if (!mounted) return;
            onNearEnd();
          });
        }
        final t = tracks[index];
        final videoId = (t['id'] ?? t['videoId'])?.toString();
        return Container(
          margin: const EdgeInsets.only(bottom: 14),
          decoration: BoxDecoration(
            color: const Color(0xFF1B1D20),
            borderRadius: BorderRadius.circular(_radiusLarge),
            border: Border.all(
              color: Colors.white.withValues(alpha: 0.06),
              width: 1,
            ),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.2),
                blurRadius: 18,
                offset: const Offset(0, 10),
              )
            ],
          ),
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(16),
              onTapDown: (_) => _warmTrack(videoId),
              onTap: () {
                final normalizedTrack = Map<String, dynamic>.from(t);
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
                    TrackMenuButton(
                      track: Map<String, dynamic>.from(t),
                      onOpenDetails: () => _openTrackDetails(
                        Map<String, dynamic>.from(t),
                        videoId,
                      ),
                      onAddToPlaylist: _addTrackToPlaylistFromMenu,
                      onStartStation: _startTrackStationFromMenu,
                      buttonSize: 42,
                      iconSize: 20,
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

    final recState = widget.searchOnly
        ? const RecommendationFeedState()
        : ref.watch(recommendationProvider);
    final isRecLoading = widget.searchOnly
        ? false
        : ref.watch(recommendationProvider.notifier).isLoading;
    final isRecPaginating = widget.searchOnly
        ? false
        : ref.watch(recommendationProvider.notifier).isPaginating;
    final lastPlayedTracks = widget.searchOnly
        ? const <Map<String, dynamic>>[]
        : _visibleLastPlayedTracks(
            ref.watch(lastPlayedProvider),
          );
    final isLastPlayedLoading = widget.searchOnly
        ? false
        : ref.watch(lastPlayedProvider.notifier).isLoading;
    final frequentTracks = widget.searchOnly
        ? const <Map<String, dynamic>>[]
        : ref.watch(frequentlyPlayedProvider);
    final isFrequentlyLoading = widget.searchOnly
        ? false
        : ref.watch(frequentlyPlayedProvider.notifier).isLoading;
    final suggestState = ref.watch(suggestProvider);
    final showSearchSuggestions = _isSearching &&
        suggestState.isNotEmpty &&
        _urlController.text.trim().isNotEmpty &&
        searchPage.requestState != 'complete' &&
        !searchPage.hasResults &&
        !isSearchLoading;
    final visibleTracks =
        _isSearching ? searchPage.tracks : recState.visibleTracks;
    final showRecentSearchHistory = _isSearching &&
        _urlController.text.trim().isEmpty &&
        !isSearchLoading &&
        !searchPage.hasResults &&
        _recentSearchHistory.isNotEmpty;

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _primeLikelyTracks(visibleTracks);
    });
    if (!widget.searchOnly) {
      _ensureRecommendationPageCanPaginate(
        recState,
        isRecLoading,
        isRecPaginating,
      );
    }
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
            padding: EdgeInsets.fromLTRB(
              16,
              16,
              16,
              widget.searchOnly ? 180 : 220,
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (_isSearching) ...[
                  Row(
                    children: [
                      if (!widget.searchOnly)
                        IconButton(
                          onPressed: () =>
                              _clearSearch(refreshRecommendations: false),
                          icon: const Icon(Icons.arrow_back_rounded),
                          color: Colors.white,
                        ),
                      Expanded(
                        child: Semantics(
                          label: 'Search music',
                          child: TextField(
                            controller: _urlController,
                            focusNode: _searchFocusNode,
                            textInputAction: TextInputAction.search,
                            style: const TextStyle(color: Colors.white),
                            decoration: InputDecoration(
                              hintText: 'Search tracks, albums, artists',
                              hintStyle: TextStyle(
                                color: Colors.white.withValues(alpha: 0.34),
                              ),
                              filled: true,
                              fillColor: Colors.white.withValues(alpha: 0.05),
                              isDense: true,
                              contentPadding: const EdgeInsets.symmetric(
                                vertical: 12,
                                horizontal: 14,
                              ),
                              border: OutlineInputBorder(
                                borderRadius:
                                    BorderRadius.circular(_radiusMedium),
                                borderSide: BorderSide.none,
                              ),
                              suffixIcon: _urlController.text.trim().isNotEmpty
                                  ? IconButton(
                                      icon: const Icon(
                                        Icons.close_rounded,
                                        color: Colors.white70,
                                      ),
                                      onPressed: () => _clearSearch(
                                        refreshRecommendations: false,
                                      ),
                                    )
                                  : null,
                            ),
                            onSubmitted: (_) => _performSearch(ref),
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                ],

                if (showSearchSuggestions)
                  Container(
                    margin: const EdgeInsets.only(bottom: 14),
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.035),
                      borderRadius: BorderRadius.circular(_radiusMedium),
                      border: Border.all(
                        color: Colors.white.withValues(alpha: 0.08),
                      ),
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
                  Tooltip(
                    message: _heroQuip,
                    child: Material(
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
                                        color: Colors.white.withValues(
                                          alpha: 0.68,
                                        ),
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
                  ),
                ],

                SizedBox(height: showSearchSuggestions ? 4 : 20),

                // Content Area (Search vs Recommendations)
                if (_isSearching) ...[
                  if (showRecentSearchHistory) ...[
                    Text(
                      'Recent searches',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.78),
                        fontSize: 14,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 0.4,
                      ),
                    ),
                    const SizedBox(height: 12),
                    ..._recentSearchHistory.map((query) {
                      return Container(
                        margin: const EdgeInsets.only(bottom: 10),
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.03),
                          borderRadius: BorderRadius.circular(_radiusMedium),
                          border: Border.all(
                            color: Colors.white.withValues(alpha: 0.08),
                          ),
                        ),
                        child: ListTile(
                          leading: const Icon(
                            Icons.history_rounded,
                            color: Colors.white70,
                          ),
                          title: Text(
                            query,
                            style: const TextStyle(color: Colors.white),
                          ),
                          trailing: Icon(
                            Icons.north_west_rounded,
                            color: Colors.white.withValues(alpha: 0.46),
                          ),
                          onTap: () => _performSearch(ref, query),
                        ),
                      );
                    }),
                    if (_isRecentSearchHistoryLoading) ...[
                      const SizedBox(height: 6),
                      const Center(
                        child: SizedBox(
                          width: 20,
                          height: 20,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        ),
                      ),
                    ],
                  ] else ...[
                    const Text('Search Results',
                        style: TextStyle(
                            fontSize: 18,
                            fontWeight: FontWeight.bold,
                            color: Colors.white)),
                    const SizedBox(height: 16),
                  ],
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
