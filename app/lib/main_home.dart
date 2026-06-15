import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'home_screen_controller.dart';
import 'logic/audio_provider.dart';
import 'logic/audio_provider_history.dart';
import 'logic/audio_provider_queue.dart';
import 'logic/audio_provider_recommendation.dart';
import 'logic/auth_provider.dart';
import 'logic/details_provider.dart';
import 'logic/interaction_events.dart';
import 'logic/playlist_provider.dart';
import 'logic/search_provider.dart';
import 'logic/song_match_provider.dart';
import 'main_details.dart';
import 'main_player.dart';
import 'screens/assistant_screen.dart';
import 'screens/personal_mix_detail_screen.dart';
import 'screens/playlist_detail_screen.dart';
import 'screens/recommendation_row_detail_screen.dart';
import 'ui/app_theme_tokens.dart';
import 'widgets/home/neatie_home_sections.dart';
import 'widgets/home/home_track_prewarm.dart';
import 'widgets/home/recent_search_history_section.dart';
import 'widgets/home/search_surface_widgets.dart';
import 'widgets/home/song_match_widgets.dart';
import 'widgets/home/track_menu_button.dart';
import 'widgets/playlist/add_to_playlist_dialog.dart';
import 'widgets/track_list_skeleton.dart';

const _accentGrey = neatieActive;
const _surfaceGreyAlt = neatieGlass;

class HomeScreen extends ConsumerStatefulWidget {
  final bool searchOnly;
  final HomeScreenController? controller;
  final VoidCallback? onOpenSearchTab;
  final VoidCallback? onOpenProfileTab;

  const HomeScreen({
    super.key,
    this.searchOnly = false,
    this.controller,
    this.onOpenSearchTab,
    this.onOpenProfileTab,
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
  final Set<String> _recordedHomeTabTaps = <String>{};
  final Map<String, String> _selectedGenreTabs = <String, String>{};
  NeatieHomeTab _selectedHomeTab = NeatieHomeTab.all;
  String _selectedSearchTab = 'Top';
  List<String> _recentSearchHistory = const <String>[];
  bool _isRecentSearchHistoryLoading = false;
  String _lastPrimeSignature = '';
  int _lastSongMatchPresentationToken = 0;
  bool _isSongMatchSheetOpen = false;
  int _lastFocusSearchRequestId = 0;
  int _lastShowHomeFeedRequestId = 0;
  int _lastHandleBackRequestId = 0;
  int _lastOpenSongMatchRequestId = 0;

  @override
  void initState() {
    super.initState();
    _isSearching = widget.searchOnly;
    widget.controller?.addListener(_handleControllerChanged);
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
      _syncControllerCanHandleBack();
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
    _syncControllerCanHandleBack();
  }

  @override
  void didUpdateWidget(covariant HomeScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller != widget.controller) {
      oldWidget.controller?.removeListener(_handleControllerChanged);
      widget.controller?.addListener(_handleControllerChanged);
      _syncControllerCanHandleBack();
    }
  }

  void _setSearchMode(bool value) {
    final nextValue = widget.searchOnly ? true : value;
    if (_isSearching == nextValue) {
      return;
    }
    setState(() => _isSearching = nextValue);
    _syncControllerCanHandleBack();
  }

  @override
  void dispose() {
    widget.controller?.removeListener(_handleControllerChanged);
    _suggestDebounce?.cancel();
    _homeScrollController
      ..removeListener(_handleHomeScroll)
      ..dispose();
    _urlController.dispose();
    _searchFocusNode.dispose();
    super.dispose();
  }

  void _handleControllerChanged() {
    final controllerState = widget.controller?.value;
    if (controllerState == null || !mounted) return;

    if (controllerState.focusSearchRequestId != _lastFocusSearchRequestId) {
      _lastFocusSearchRequestId = controllerState.focusSearchRequestId;
      focusSearch();
    }
    if (controllerState.showHomeFeedRequestId != _lastShowHomeFeedRequestId) {
      _lastShowHomeFeedRequestId = controllerState.showHomeFeedRequestId;
      showHomeFeed();
    }
    if (controllerState.handleBackRequestId != _lastHandleBackRequestId) {
      _lastHandleBackRequestId = controllerState.handleBackRequestId;
      handleSystemBack();
    }
    if (controllerState.openSongMatchRequestId != _lastOpenSongMatchRequestId) {
      _lastOpenSongMatchRequestId = controllerState.openSongMatchRequestId;
      unawaited(
        openSongMatchSheet(
          preferPendingShared: controllerState.preferPendingSharedForSongMatch,
        ),
      );
    }
  }

  void _syncControllerCanHandleBack() {
    widget.controller?.updateCanHandleBack(handleSystemBackCandidate);
  }

  void _handleHomeScroll() {
    if (_isSearching || !_homeScrollController.hasClients) return;
    // Quiet Picks enrichment is intentionally user-driven. Extending it from
    // generic page-bottom scroll made home latency feel unpredictable.
  }

  bool _handleHomeScrollNotification(ScrollNotification notification) {
    if (notification.metrics.axis == Axis.vertical) {
      _handleHomeScroll();
    }
    return false;
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
    _syncControllerCanHandleBack();
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
    _syncControllerCanHandleBack();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _searchFocusNode.requestFocus();
      _urlController.selection = TextSelection.fromPosition(
        TextPosition(offset: _urlController.text.length),
      );
    });
  }

  Future<void> openSongMatchSheet({bool preferPendingShared = false}) async {
    if (!mounted || _isSongMatchSheetOpen) return;
    _setSearchMode(true);
    _isSongMatchSheetOpen = true;
    final notifier = ref.read(songMatchProvider.notifier);
    try {
      await showSongMatchOptionsSheet(
        context: context,
        songMatchState: ref.read(songMatchProvider),
        preferPendingShared: preferPendingShared,
        onUsePendingShared: () {
          unawaited(notifier.matchPendingSharedFile());
        },
        onRecordSnippet: () {
          unawaited(notifier.startRecordingSnippet());
        },
        onChooseFile: () {
          unawaited(notifier.pickFileAndMatch());
        },
      );
    } finally {
      _isSongMatchSheetOpen = false;
    }
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
    unawaited(
      ref.read(recommendationProvider.notifier).applyPreparedFeedOnHomeReturn(),
    );
    _syncControllerCanHandleBack();
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

  bool handleSystemBack() {
    if (widget.searchOnly) {
      if (handleSystemBackCandidate) {
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

  bool get handleSystemBackCandidate {
    if (widget.searchOnly) {
      return _urlController.text.trim().isNotEmpty ||
          ref.read(searchPageProvider).hasResults ||
          ref.read(suggestProvider).isNotEmpty;
    }
    return hasActiveSearch;
  }

  bool get hasActiveSearch =>
      _isSearching || _urlController.text.trim().isNotEmpty;

  void _primeLikelyTracks(List<dynamic> tracks) {
    final idsToWarm = collectHomePrewarmTrackIds(
      tracks: tracks,
      alreadyPrewarmed: _prewarmedTrackIds,
    );
    if (idsToWarm.isEmpty) return;

    final signature = homePrewarmSignature(idsToWarm);
    if (signature == _lastPrimeSignature) return;
    _lastPrimeSignature = signature;

    unawaited(
      ref.read(audioPlayerProvider.notifier).prewarmStreams(
            idsToWarm,
            lookahead: math.max(10, idsToWarm.length),
            immediatePlayback: true,
            currentVideoId: idsToWarm.first,
          ),
    );
  }

  Future<void> _openMatchedTrackActions(Map<String, dynamic> track) async {
    if (!mounted) return;
    final normalizedTrack = normalizeTrack(track);
    await showTrackActionSheet(
      context: context,
      ref: ref,
      track: normalizedTrack,
      onOpenDetails: () =>
          _openTrackDetails(normalizedTrack, extractTrackId(normalizedTrack)),
      onAddToPlaylist: _addTrackToPlaylistFromMenu,
      onStartStation: _startTrackStationFromMenu,
    );
  }

  Future<void> _showSongMatchAlternatives(
    List<Map<String, dynamic>> alternatives,
  ) async {
    if (!mounted) return;
    await showSongMatchAlternativesSheet(
      context: context,
      alternatives: alternatives,
      onSelectAlternative: (track) async {
        if (!mounted) return;
        await _openMatchedTrackActions(normalizeTrack(track));
      },
    );
  }

  Widget _buildSongMatchLauncher(SongMatchState songMatchState) {
    return SongMatchLauncher(
      songMatchState: songMatchState,
      onOpen: () => openSongMatchSheet(
        preferPendingShared: songMatchState.pendingSharedMedia != null,
      ),
      onCancelRecording: () {
        ref.read(songMatchProvider.notifier).cancelRecording();
      },
      onStopRecording: () {
        ref.read(songMatchProvider.notifier).stopRecordingAndMatch();
      },
    );
  }

  void _warmTrack(String? videoId) {
    if (videoId == null || videoId.isEmpty) return;
    _prewarmedTrackIds.add(videoId);
    unawaited(
      ref.read(audioPlayerProvider.notifier).prepareImmediatePlayback(videoId),
    );
  }

  void _selectHomeTab(
    RecommendationFeedState feedState,
    NeatieHomeTab tab,
  ) {
    setState(() => _selectedHomeTab = tab);
    if (tab == NeatieHomeTab.all || feedState.sessionId.isEmpty) return;
    final tracks = _backendHomeTabTracks(feedState, tab);
    if (tracks.isEmpty) return;
    final track = tracks.first;
    final trackId = extractTrackId(track);
    if (trackId == null || trackId.isEmpty) return;
    final key = '${feedState.sessionId}:${tab.id}';
    if (!_recordedHomeTabTaps.add(key)) return;
    unawaited(
      recordProxyInteractionEvent(
        'tab_tap',
        trackId: trackId,
        rawTrack: track,
        metadata: {
          'surface': 'home',
          'tab_id': tab.id,
          'session_id': feedState.sessionId,
        },
      ),
    );
  }

  List<Map<String, dynamic>> _dedupeTracks(List<Map<String, dynamic>> tracks) {
    final seen = <String>{};
    final output = <Map<String, dynamic>>[];
    for (final track in tracks) {
      final id = extractTrackId(track) ??
          '${track['title'] ?? ''}|${track['channel'] ?? track['artist'] ?? ''}';
      final key = id.trim().toLowerCase();
      if (key.isEmpty || seen.contains(key)) continue;
      seen.add(key);
      output.add(Map<String, dynamic>.from(track));
    }
    return output;
  }

  List<Map<String, dynamic>> _tracksForHomeTab(
    RecommendationFeedState feedState,
    NeatieHomeTab tab,
  ) {
    final backendLane = _backendHomeTabTracks(feedState, tab);
    if (backendLane.isNotEmpty) {
      return backendLane;
    }
    final all = _dedupeTracks([
      ...rowTracks(feedState, const ['todays_pick']),
      ..._madeForYouTiles(feedState).expand((tile) => tile.tracks),
      ...rowTracks(feedState, const ['quiet_picks']),
      ...rowTracks(feedState, const ['because_you_played']),
      ...rowTracks(feedState, const ['hidden_gems']),
      ...rowTracks(feedState, const ['trending_by_genre']),
    ]);
    if (tab == NeatieHomeTab.all) return all;
    final keywordMap = <NeatieHomeTab, List<String>>{
      NeatieHomeTab.chill: ['acoustic', 'soft', 'slow', 'quiet', 'chill', 'soul'],
      NeatieHomeTab.workout: ['rock', 'dance', 'remix', 'live', 'power', 'beat'],
      NeatieHomeTab.focus: ['instrumental', 'ambient', 'piano', 'study', 'focus'],
      NeatieHomeTab.mood: ['love', 'night', 'dream', 'sad', 'happy', 'mood'],
    };
    final keywords = keywordMap[tab] ?? const <String>[];
    final scored = <MapEntry<Map<String, dynamic>, int>>[];
    for (final track in all) {
      final haystack = [
        track['title'],
        track['channel'],
        track['artist'],
        track['album'],
        track['recommendation_reason'],
      ].whereType<Object>().join(' ').toLowerCase();
      final score = keywords.where(haystack.contains).length;
      scored.add(MapEntry(track, score));
    }
    scored.sort((a, b) => b.value.compareTo(a.value));
    return scored.map((entry) => entry.key).take(24).toList(growable: false);
  }

  List<Map<String, dynamic>> _backendHomeTabTracks(
    RecommendationFeedState feedState,
    NeatieHomeTab tab,
  ) {
    return _backendHomeTabItems(feedState, tab, 'tracks');
  }

  List<Map<String, dynamic>> _backendHomeTabItems(
    RecommendationFeedState feedState,
    NeatieHomeTab tab,
    String collection,
  ) {
    final lanes = feedState.diagnostics['home_tab_lanes'];
    if (lanes is! Map) return const <Map<String, dynamic>>[];
    final laneId = tab.id;
    final lane = lanes[laneId];
    final rawItems = lane is Map
        ? lane[collection]
        : collection == 'tracks'
            ? lane
            : null;
    if (rawItems is! List) return const <Map<String, dynamic>>[];
    final items = rawItems
          .whereType<Map>()
          .map((item) => Map<String, dynamic>.from(item))
          .toList(growable: false);
    return collection == 'tracks' || collection == 'discoveries'
        ? _dedupeTracks(items)
        : items;
  }

  int _homeTabMinItems(RecommendationFeedState feedState) {
    final diagnostics = feedState.diagnostics['home_tab_diagnostics'];
    if (diagnostics is Map) {
      final minCount = diagnostics['min_count'];
      if (minCount is num && minCount.toInt() > 0) {
        return minCount.toInt();
      }
    }
    return 8;
  }

  bool _hasAcceptedBackendHomeTabs(RecommendationFeedState feedState) {
    final diagnostics = feedState.diagnostics['home_tab_diagnostics'];
    return diagnostics is Map && diagnostics['accepted'] == true;
  }

  Set<NeatieHomeTab> _availableHomeTabs(RecommendationFeedState feedState) {
    final tabs = <NeatieHomeTab>{};
    final minItems = _homeTabMinItems(feedState);
    final hasAcceptedBackendTabs = _hasAcceptedBackendHomeTabs(feedState);
    final visibleMinItems =
        hasAcceptedBackendTabs ? minItems : math.min(minItems, 6);
    for (final tab in NeatieHomeTab.values) {
      if (tab == NeatieHomeTab.all) continue;
      final backendLaneCount = _backendHomeTabTracks(feedState, tab).length;
      final tabTrackCount = hasAcceptedBackendTabs
          ? _tracksForHomeTab(feedState, tab).length
          : backendLaneCount;
      if (tabTrackCount >= visibleMinItems) {
        tabs.add(tab);
      }
    }
    return tabs;
  }

  List<NeatieMixTileData> _madeForYouTiles(
    RecommendationFeedState feedState,
  ) {
    final rows = feedState.rows;
    final tiles = <NeatieMixTileData>[];
    for (final row in rows) {
      if (row.kind != 'made_for_you') {
        continue;
      }
      if (row.rowStyle == 'mix_cards') {
        for (final mix in row.items.take(4)) {
          final tracks = _mixTracks(mix);
          tiles.add(
            NeatieMixTileData(
              title: mix['title']?.toString() ?? 'Daily Mix',
              subtitle:
                  mix['subtitle']?.toString() ?? 'Your daily picks updated',
              tracks: tracks,
              mix: Map<String, dynamic>.from(mix),
              thumbnail: mix['thumbnail'],
              videoId: tracks.isNotEmpty ? extractTrackId(tracks.first) : null,
            ),
          );
        }
      } else if (row.items.isNotEmpty) {
        final tracks = _dedupeTracks(row.items).take(12).toList(growable: false);
        tiles.add(
          NeatieMixTileData(
            title: row.title.isNotEmpty ? row.title : 'Made for you',
            subtitle: 'Taste-shaped picks from your orbit.',
            tracks: tracks,
            mix: <String, dynamic>{
              'id': row.id,
              'title': row.title,
              'subtitle': 'Taste-shaped picks from your orbit.',
              'description':
                  'A focused playlist built from your recent listening.',
            },
            thumbnail: tracks.isNotEmpty ? tracks.first['thumbnail'] : null,
            videoId: tracks.isNotEmpty ? extractTrackId(tracks.first) : null,
          ),
        );
      }
    }
    return tiles.take(8).toList(growable: false);
  }

  List<Map<String, dynamic>> _mixTracks(Map<String, dynamic> mix) {
    final rawTracks = mix['tracks'] ?? mix['items'] ?? mix['recommendations'];
    if (rawTracks is! List) {
      return const <Map<String, dynamic>>[];
    }
    return _dedupeTracks(
      rawTracks
          .whereType<Map>()
          .map((track) => normalizeTrack(Map<String, dynamic>.from(track)))
          .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
          .toList(growable: false),
    );
  }

  List<Map<String, dynamic>> _genreTabTracks(RecommendationFeedState feedState) {
    final genreRow = _feedRowByKind(feedState, 'trending_by_genre');
    if (genreRow == null) return const <Map<String, dynamic>>[];
    final tracks = <Map<String, dynamic>>[];
    for (final tab in _genreTabsFromRow(genreRow)) {
      for (final track in _genreTracksForTab(tab)) {
        tracks.add(track);
      }
    }
    tracks.addAll(genreRow.items);
    return _dedupeTracks(tracks);
  }

  RecommendationFeedRowState? _feedRowByKind(
    RecommendationFeedState feedState,
    String kind,
  ) {
    for (final row in feedState.rows) {
      if (row.kind == kind) return row;
    }
    return null;
  }

  RecommendationFeedRowState _syntheticRow({
    required String id,
    required String title,
    required String itemType,
    required List<Map<String, dynamic>> items,
  }) {
    return RecommendationFeedRowState(
      id: id,
      title: title,
      kind: id,
      itemType: itemType,
      rowStyle: 'lane_detail',
      meta: const <String, dynamic>{},
      items: items,
      nextOffset: items.length,
      hasMore: false,
    );
  }

  List<Map<String, dynamic>> _genreTabsFromRow(
    RecommendationFeedRowState? row,
  ) {
    final tabs = row?.meta['tabs'];
    if (tabs is! List) return const <Map<String, dynamic>>[];
    final seen = <String>{};
    final output = <Map<String, dynamic>>[];
    for (final tab in tabs.whereType<Map>()) {
      final next = Map<String, dynamic>.from(tab);
      final id = next['id']?.toString().trim();
      final label = next['label']?.toString().trim();
      final key = (id?.isNotEmpty == true ? id : label)?.toLowerCase() ?? '';
      if (key.isEmpty || !seen.add(key)) continue;
      if (_genreTracksForTab(next).isEmpty) continue;
      output.add(next);
    }
    return output;
  }

  List<Map<String, dynamic>> _genreTracksForTab(Map<String, dynamic> tab) {
    return (tab['tracks'] as List<dynamic>? ?? const [])
        .whereType<Map>()
        .map((track) => Map<String, dynamic>.from(track))
        .toList(growable: false);
  }

  String _selectedGenreTabIdForRow(
    RecommendationFeedRowState row,
    List<Map<String, dynamic>> tabs,
  ) {
    final selected = _selectedGenreTabs[row.id]?.trim();
    if (selected != null &&
        selected.isNotEmpty &&
        tabs.any((tab) => tab['id']?.toString() == selected)) {
      return selected;
    }
    return tabs.first['id']?.toString() ?? '';
  }

  List<Map<String, dynamic>> _albumPreviewTracks(Map<String, dynamic> album) {
    final rawTracks = album['tracks'];
    if (rawTracks is List) {
      final tracks = rawTracks
          .whereType<Map>()
          .map((track) => normalizeTrack(Map<String, dynamic>.from(track)))
          .where((track) => extractTrackId(track)?.isNotEmpty ?? false)
          .toList(growable: false);
      if (tracks.isNotEmpty) return tracks;
    }
    final preview = album['preview_track'];
    if (preview is Map) {
      final track = normalizeTrack(Map<String, dynamic>.from(preview));
      if (extractTrackId(track)?.isNotEmpty ?? false) {
        return [track];
      }
    }
    final sourceTrackId = album['source_track_id']?.toString().trim();
    if (sourceTrackId != null && sourceTrackId.isNotEmpty) {
      final fallbackTrack = normalizeTrack({
        ...album,
        'id': sourceTrackId,
        'videoId': sourceTrackId,
        'channel': album['artist'] ?? album['channel'],
        'artist': album['artist'] ?? album['channel'],
      });
      if (extractTrackId(fallbackTrack)?.isNotEmpty ?? false) {
        return [fallbackTrack];
      }
    }
    return const <Map<String, dynamic>>[];
  }

  Future<void> _playAlbumPreview(Map<String, dynamic> album) async {
    final tracks = _albumPreviewTracks(album);
    if (tracks.isEmpty) {
      await _openAlbum(album);
      return;
    }
    final title = album['title']?.toString().trim();
    final albumId = album['id']?.toString().trim();
    final previewId = albumId?.isNotEmpty == true
        ? albumId!
        : title?.isNotEmpty == true
            ? title!
            : 'album';
    await _playTrackFromCollection(
      tracks: tracks,
      track: tracks.first,
      playlistId: 'neatie:album_preview:$previewId',
      playlistName: title?.isNotEmpty == true ? title! : 'Album preview',
    );
  }

  Future<void> _playTrackList({
    required List<Map<String, dynamic>> tracks,
    required Map<String, dynamic> track,
    required String playlistId,
    required String playlistName,
  }) {
    return _playTrackFromCollection(
      tracks: tracks,
      track: track,
      playlistId: playlistId,
      playlistName: playlistName,
    );
  }

  Future<void> _openRecommendationRow(
    RecommendationFeedRowState? row,
  ) async {
    if (row == null || row.items.isEmpty || !mounted) return;
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => RecommendationRowDetailScreen(
          initialRow: row,
          playerScreenBuilder: (_) => const FullPlayerScreen(),
          trackDetailsScreenBuilder: (track) => TrackDetailsScreen(track: track),
          onPrimeTrack: _warmTrack,
          onOpenAlbum: (album) => unawaited(_openAlbum(album)),
          onOpenArtist: (artist) => unawaited(_openArtist(artist)),
          onOpenMix: (mix) => unawaited(_openPersonalMixItem(mix)),
        ),
      ),
    );
  }

  Future<void> _openPersonalMix(NeatieMixTileData tile) async {
    if (tile.tracks.isEmpty || !mounted) return;
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => PersonalMixDetailScreen(
          mix: tile.mix,
          tracks: tile.tracks,
          playerScreenBuilder: (_) => const FullPlayerScreen(),
          trackDetailsScreenBuilder: (track) => TrackDetailsScreen(track: track),
          onPrimeTrack: _warmTrack,
        ),
      ),
    );
  }

  Future<void> _openPersonalMixItem(Map<String, dynamic> mix) async {
    final tracks = _mixTracks(mix);
    if (tracks.isEmpty) return;
    await _openPersonalMix(
      NeatieMixTileData(
        title: mix['title']?.toString() ?? 'Mix for you',
        subtitle: mix['subtitle']?.toString() ?? 'Built from your listening.',
        tracks: tracks,
        mix: Map<String, dynamic>.from(mix),
        thumbnail: mix['thumbnail'],
        videoId: extractTrackId(tracks.first),
      ),
    );
  }

  Future<void> _playSearchDiscoveryTrack(Map<String, dynamic> track) async {
    await ref.read(playbackQueueProvider.notifier).startDiscoverySession(
          track,
          sessionName:
              'Inspired by ${track['title']?.toString() ?? 'this track'}',
        );
    if (!mounted) return;
    await Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => const FullPlayerScreen()),
    );
  }

  Widget _buildNeatieHomeExperience({
    required RecommendationFeedState recState,
    required List<Map<String, dynamic>> lastPlayedTracks,
    required List<Map<String, dynamic>> frequentTracks,
    required String displayName,
    required String? avatarUrl,
    required bool isLoading,
  }) {
    final availableTabs = _availableHomeTabs(recState);
    final selectedTab = _selectedHomeTab == NeatieHomeTab.all ||
            availableTabs.contains(_selectedHomeTab)
        ? _selectedHomeTab
        : NeatieHomeTab.all;
    final tabTracks = _tracksForHomeTab(recState, selectedTab);
    final heroAlbums = rowAlbums(
      recState,
      const ['featured_new_albums', 'recommended_albums'],
    );
    final todaysPickTracks = rowTracks(recState, const ['todays_pick']);
    final recommendedAlbums = rowAlbums(recState, const ['recommended_albums']);
    final globalArtists = rowArtists(recState, const ['recommended_artists']);
    final becauseYouPlayedTracks =
        rowTracks(recState, const ['because_you_played']);
    final quietPickTracks = rowTracks(recState, const ['quiet_picks']);
    final hiddenGemTracks = rowTracks(recState, const ['hidden_gems']);
    final trendingTracks = selectedTab == NeatieHomeTab.all
        ? _genreTabTracks(recState)
        : tabTracks;
    const trendingTitle = 'Trending by genre';
    final genreRow = _feedRowByKind(recState, 'trending_by_genre');
    final todaysPickRow = _feedRowByKind(recState, 'todays_pick');
    final lastPlayedRow = _feedRowByKind(recState, 'last_played');
    final madeForYouRow = _feedRowByKind(recState, 'made_for_you');
    final becauseYouPlayedRow = _feedRowByKind(recState, 'because_you_played');
    final hiddenGemsRow = _feedRowByKind(recState, 'hidden_gems');
    final frequentRow = _feedRowByKind(recState, 'frequently_listened');
    final quietPicksRow = _feedRowByKind(recState, 'quiet_picks');
    final recommendedAlbumsRow = _feedRowByKind(recState, 'recommended_albums');
    final recommendedArtistsRow =
        _feedRowByKind(recState, 'recommended_artists');
    final genreTabs = _genreTabsFromRow(genreRow);
    final selectedGenreTabId = genreRow == null || genreTabs.isEmpty
        ? ''
        : _selectedGenreTabIdForRow(genreRow, genreTabs);
    final selectedGenreTracks = genreTabs.isEmpty
        ? const <Map<String, dynamic>>[]
        : _genreTracksForTab(
            genreTabs.firstWhere(
              (tab) => tab['id']?.toString() == selectedGenreTabId,
              orElse: () => genreTabs.first,
            ),
          );
    final laneAlbums = _backendHomeTabItems(recState, selectedTab, 'albums');
    final laneArtists = _backendHomeTabItems(recState, selectedTab, 'artists');
    final visibleAlbums = selectedTab == NeatieHomeTab.all
        ? recommendedAlbums
        : laneAlbums;
    final visibleArtists =
        selectedTab == NeatieHomeTab.all ? globalArtists : laneArtists;
    final laneDiscoveryRow = _syntheticRow(
      id: 'lane:${selectedTab.id}:discoveries',
      title: 'Discoveries',
      itemType: 'track',
      items: tabTracks,
    );
    final laneAlbumRow = _syntheticRow(
      id: 'lane:${selectedTab.id}:albums',
      title: 'Albums',
      itemType: 'album',
      items: visibleAlbums,
    );
    final laneArtistRow = _syntheticRow(
      id: 'lane:${selectedTab.id}:artists',
      title: 'Artists to explore',
      itemType: 'artist',
      items: visibleArtists,
    );
    final madeForYou = _madeForYouTiles(recState);
    final recentTracks = selectedTab == NeatieHomeTab.all
        ? lastPlayedTracks
        : tabTracks.take(10).toList(growable: false);

    void playTrack(
      Map<String, dynamic> track,
      List<Map<String, dynamic>> source,
      String name,
    ) {
      unawaited(
        _playTrackList(
          tracks: source,
          track: track,
          playlistId: 'neatie:${selectedTab.id}:$name',
          playlistName: name,
        ),
      );
    }

    if (isLoading && recState.rows.isEmpty) {
      return AnimatedSwitcher(
        duration: const Duration(milliseconds: 260),
        switchInCurve: Curves.easeOutCubic,
        child: Column(
          key: const ValueKey<String>('neatie-home-loading'),
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            NeatieHomeTopBar(
              displayName: displayName,
              logoAsset: 'assets/branding/neatie_intro_mark.png',
              avatarUrl: avatarUrl,
              onSearch: widget.onOpenSearchTab ?? focusSearch,
              onProfile: widget.onOpenProfileTab ?? _openAssistant,
            ),
            const SizedBox(height: 18),
            const TrackListSkeleton(count: 5),
          ],
        ),
      );
    }

    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 280),
      switchInCurve: Curves.easeOutCubic,
      child: Column(
        key: ValueKey<String>(
          'neatie-home:${recState.sessionId}:${recState.rows.length}:${selectedTab.id}',
        ),
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          NeatieHomeTopBar(
            displayName: displayName,
            logoAsset: 'assets/branding/neatie_intro_mark.png',
            avatarUrl: avatarUrl,
            onSearch: widget.onOpenSearchTab ?? focusSearch,
            onProfile: widget.onOpenProfileTab ?? _openAssistant,
          ),
        NeatieFeaturedAlbumHero(
          albums: heroAlbums,
          onOpen: (album) => unawaited(_openAlbum(album)),
          onPlay: (album) => unawaited(_playAlbumPreview(album)),
        ),
          const SizedBox(height: 12),
          NeatieHomeTabBar(
            selected: selectedTab,
            availableTabs: availableTabs,
            onSelected: (tab) => _selectHomeTab(recState, tab),
          ),
        if (selectedTab == NeatieHomeTab.all && todaysPickTracks.isNotEmpty) ...[
          const SizedBox(height: 14),
          NeatieTrackStrip(
            title: "Today's pick",
            tracks: todaysPickTracks,
            onPlay: (track) =>
                playTrack(track, todaysPickTracks, "Today's pick"),
            onMenuDetails: (track) =>
                unawaited(_openTrackDetails(track, extractTrackId(track))),
            onViewAll: () => unawaited(_openRecommendationRow(todaysPickRow)),
          ),
        ],
        const SizedBox(height: 14),
        if (selectedTab == NeatieHomeTab.all)
          NeatieTrackStrip(
            title: 'Recently played',
            tracks: recentTracks,
            onPlay: (track) => playTrack(track, recentTracks, 'Recently played'),
            onMenuDetails: (track) =>
                unawaited(_openTrackDetails(track, extractTrackId(track))),
            onViewAll: () => unawaited(_openRecommendationRow(lastPlayedRow)),
          )
        else
          NeatieLongTrackList(
            title: 'Discoveries',
            tracks: tabTracks,
            onPlay: (track) =>
                playTrack(track, tabTracks, 'Discoveries'),
            onMenuDetails: (track) =>
                unawaited(_openTrackDetails(track, extractTrackId(track))),
            onViewAll: () => unawaited(_openRecommendationRow(laneDiscoveryRow)),
          ),
        if (selectedTab == NeatieHomeTab.all && madeForYou.isNotEmpty) ...[
          const SizedBox(height: 18),
          NeatieMadeForYouRow(
            items: madeForYou,
            onPlay: (tile) => unawaited(_openPersonalMix(tile)),
            onViewAll: () => unawaited(_openRecommendationRow(madeForYouRow)),
          ),
        ],
        if (selectedTab == NeatieHomeTab.all &&
            becauseYouPlayedTracks.isNotEmpty) ...[
          const SizedBox(height: 18),
          NeatieTrackStrip(
            title: 'Because you played',
            tracks: becauseYouPlayedTracks,
            onPlay: (track) =>
                playTrack(track, becauseYouPlayedTracks, 'Because you played'),
            onMenuDetails: (track) =>
                unawaited(_openTrackDetails(track, extractTrackId(track))),
            onViewAll: () =>
                unawaited(_openRecommendationRow(becauseYouPlayedRow)),
          ),
        ],
        if (selectedTab == NeatieHomeTab.all && hiddenGemTracks.isNotEmpty) ...[
          const SizedBox(height: 18),
          NeatieTrackStrip(
            title: 'Hidden gems',
            tracks: hiddenGemTracks,
            onPlay: (track) => playTrack(track, hiddenGemTracks, 'Hidden gems'),
            onMenuDetails: (track) =>
                unawaited(_openTrackDetails(track, extractTrackId(track))),
            onViewAll: () => unawaited(_openRecommendationRow(hiddenGemsRow)),
          ),
        ],
        if (selectedTab == NeatieHomeTab.all && genreTabs.isNotEmpty) ...[
          const SizedBox(height: 18),
          NeatieGenreTabsRow(
            title: 'Trending by genre',
            tabs: genreTabs,
            selectedTabId: selectedGenreTabId,
            onSelectedTab: (tabId) {
              if (genreRow == null) return;
              setState(() => _selectedGenreTabs[genreRow.id] = tabId);
            },
            onPlay: (track) =>
                playTrack(track, selectedGenreTracks, 'Trending by genre'),
            onMenuDetails: (track) =>
                unawaited(_openTrackDetails(track, extractTrackId(track))),
            onViewAll: () => unawaited(_openRecommendationRow(genreRow)),
          ),
        ] else if (selectedTab == NeatieHomeTab.all &&
            trendingTracks.isNotEmpty) ...[
          const SizedBox(height: 18),
          NeatieTrendingCompactRow(
            title: trendingTitle,
            tracks: trendingTracks,
            onPlay: (track) =>
                playTrack(track, trendingTracks, trendingTitle),
            onMenuDetails: (track) =>
                unawaited(_openTrackDetails(track, extractTrackId(track))),
          ),
        ],
        if (visibleAlbums.isNotEmpty) ...[
          const SizedBox(height: 18),
          NeatieAlbumStrip(
            title: selectedTab == NeatieHomeTab.all
                ? 'Albums for you'
                : 'Albums',
            albums: visibleAlbums,
            onOpen: (album) => unawaited(_openAlbum(album)),
            onViewAll: selectedTab == NeatieHomeTab.all
                ? () => unawaited(_openRecommendationRow(recommendedAlbumsRow))
                : () => unawaited(_openRecommendationRow(laneAlbumRow)),
          ),
        ],
        if (visibleArtists.isNotEmpty) ...[
          const SizedBox(height: 18),
          NeatieArtistStrip(
            artists: visibleArtists,
            onOpen: (artist) => unawaited(_openArtist(artist)),
            onViewAll: selectedTab == NeatieHomeTab.all
                ? () => unawaited(_openRecommendationRow(recommendedArtistsRow))
                : () => unawaited(_openRecommendationRow(laneArtistRow)),
          ),
        ],
        if (selectedTab == NeatieHomeTab.all && frequentTracks.isNotEmpty) ...[
          const SizedBox(height: 18),
          NeatieTrackStrip(
            title: 'Frequently listened',
            tracks: frequentTracks,
            onPlay: (track) =>
                playTrack(track, frequentTracks, 'Frequently listened'),
            onMenuDetails: (track) =>
                unawaited(_openTrackDetails(track, extractTrackId(track))),
            onViewAll: () => unawaited(_openRecommendationRow(frequentRow)),
          ),
        ],
        if (selectedTab == NeatieHomeTab.all && quietPickTracks.isNotEmpty) ...[
          const SizedBox(height: 22),
          NeatieLongTrackList(
            title: 'Quiet Picks',
            tracks: quietPickTracks,
            onPlay: (track) => playTrack(track, quietPickTracks, 'Quiet Picks'),
            onMenuDetails: (track) =>
                unawaited(_openTrackDetails(track, extractTrackId(track))),
            onViewAll: () => unawaited(_openRecommendationRow(quietPicksRow)),
          ),
        ],
        ],
      ),
    );
  }

  Future<void> _openTrackDetails(
      Map<String, dynamic> track, String? videoId) async {
    if (videoId == null || videoId.isEmpty) return;
    _warmTrack(videoId);
    unawaited(ref.read(trackDetailsProvider.notifier).fetchDetails(videoId));
    await Navigator.push(
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
    final resolvedArtist = await resolveArtistReference(ref.read, artist);
    final artistId = resolvedArtist?['id']?.toString().trim() ?? '';
    if (!mounted) return;
    if (resolvedArtist == null || artistId.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Artist details are not available yet.')),
      );
      return;
    }
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => ArtistDetailsScreen(
          artistId: artistId,
          fallbackArtist: resolvedArtist,
        ),
      ),
    );
  }

  Future<void> _openPlaylist(Map<String, dynamic> playlist) async {
    if (playlist['generated'] == true) {
      await _openPersonalMixItem({
        ...playlist,
        'title': playlist['name'] ?? playlist['title'],
        'subtitle': playlist['subtitle'] ?? 'Search discovery mix',
      });
      return;
    }
    final playlistId = playlist['id']?.toString().trim();
    if (playlistId == null || playlistId.isEmpty) return;
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => PlaylistDetailScreen(playlistId: playlistId),
      ),
    );
  }

  List<Map<String, dynamic>> _searchLocalPlaylists(
    List<Playlist> playlists,
    String query,
  ) {
    final normalizedQuery = query.trim().toLowerCase();
    if (normalizedQuery.isEmpty) return const [];
    return playlists.where((playlist) {
      if (playlist.name.toLowerCase().contains(normalizedQuery)) return true;
      return playlist.tracks.any((rawTrack) {
        if (rawTrack is! Map) return false;
        final title = rawTrack['title']?.toString().toLowerCase() ?? '';
        final artist = (rawTrack['artist'] ??
                rawTrack['channel'] ??
                rawTrack['author'])
            ?.toString()
            .toLowerCase() ??
            '';
        return title.contains(normalizedQuery) ||
            artist.contains(normalizedQuery);
      });
    }).map((playlist) {
      final firstTrack =
          playlist.tracks.isEmpty || playlist.tracks.first is! Map
              ? null
              : Map<String, dynamic>.from(playlist.tracks.first as Map);
      return <String, dynamic>{
        'id': playlist.id,
        'name': playlist.name,
        'track_count': playlist.tracks.length,
        'thumbnail': firstTrack?['thumbnail'],
      };
    }).toList(growable: false);
  }

  List<Map<String, dynamic>> _searchDiscoveryPlaylists(
    SearchPageState searchPage,
    String query,
  ) {
    final cleanedQuery = query.trim();
    if (cleanedQuery.isEmpty) return const <Map<String, dynamic>>[];
    final primary = _dedupeTracks([
      ...searchPage.tracks,
      ...searchPage.similarTracks,
    ]);
    final artistOrbit = _dedupeTracks([
      ...searchPage.artistTracks,
      ...searchPage.similarTracks,
      ...searchPage.tracks,
    ]);
    final output = <Map<String, dynamic>>[];

    void addMix(String id, String name, String subtitle,
        List<Map<String, dynamic>> tracks) {
      final unique = _dedupeTracks(tracks).take(32).toList(growable: false);
      if (unique.length < 4) return;
      output.add({
        'id': 'search_mix:$id',
        'name': name,
        'title': name,
        'subtitle': subtitle,
        'generated': true,
        'track_count': unique.length,
        'tracks': unique,
        'thumbnail': unique.first['thumbnail'],
      });
    }

    addMix(
      'query:${cleanedQuery.toLowerCase()}',
      '$cleanedQuery mix',
      'Tracks and close neighbors from this search.',
      primary,
    );
    if (searchPage.artists.isNotEmpty) {
      final artistName =
          searchPage.artists.first['name']?.toString().trim() ?? '';
      if (artistName.isNotEmpty) {
        addMix(
          'artist:${artistName.toLowerCase()}',
          '$artistName and related',
          'Works and adjacent artists connected to $artistName.',
          artistOrbit,
        );
      }
    }
    if (searchPage.similarTracks.length >= 4) {
      addMix(
        'neighbors:${cleanedQuery.toLowerCase()}',
        'Beyond $cleanedQuery',
        'A wider discovery path around this result.',
        searchPage.similarTracks,
      );
    }
    return output;
  }

  void _openBrowseSurface(WidgetRef ref, String surface) {
    final query = switch (surface) {
      'New releases' => 'new releases',
      'Charts' => 'popular music',
      'Mood' => 'mood music',
      'Genres' => 'music genres',
      _ => surface,
    };
    _selectedSearchTab = 'Top';
    unawaited(_performSearch(ref, query));
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

  @override
  Widget build(BuildContext context) {
    final searchPage = ref.watch(searchPageProvider);
    final playlists = ref.watch(playlistProvider);
    final isSearchLoading = ref.watch(searchPageProvider.notifier).isLoading;
    final fallbackAlbums = _isSearching
        ? _deriveAlbumsFromTracks(searchPage.tracks)
        : const <Map<String, dynamic>>[];
    final displayAlbums =
        searchPage.albums.isNotEmpty ? searchPage.albums : fallbackAlbums;
    final searchTrackIds = searchPage.tracks
        .map((track) => extractTrackId(track) ?? '')
        .where((id) => id.trim().isNotEmpty)
        .toSet();
    final similarTracks = _isSearching
        ? searchPage.similarTracks
            .where((track) => !searchTrackIds.contains(extractTrackId(track)))
            .toList(growable: false)
        : const <Map<String, dynamic>>[];
    final recState = widget.searchOnly
        ? const RecommendationFeedState()
        : ref.watch(recommendationProvider);
    final authState = ref.watch(authProvider);
    final displayName =
        authState.user?.userMetadata?['full_name']?.toString() ??
            authState.user?.userMetadata?['name']?.toString() ??
            authState.user?.email?.split('@').first ??
            'Listener';
    final avatarUrl =
        authState.user?.userMetadata?['avatar_url']?.toString() ??
            authState.user?.userMetadata?['picture']?.toString();
    final isRecLoading = widget.searchOnly
        ? false
        : ref.watch(recommendationProvider.notifier).isLoading;
    final lastPlayedTracks = widget.searchOnly
        ? const <Map<String, dynamic>>[]
        : ref.watch(lastPlayedProvider);
    final frequentTracks = widget.searchOnly
        ? const <Map<String, dynamic>>[]
        : ref.watch(frequentlyPlayedProvider);
    final suggestState = ref.watch(suggestProvider);
    final showSearchSuggestions = _isSearching &&
        suggestState.isNotEmpty &&
        _urlController.text.trim().isNotEmpty &&
        searchPage.requestState != 'complete' &&
        !searchPage.hasResults &&
        !isSearchLoading;
    final visibleTracks =
        _isSearching ? searchPage.tracks : recState.visibleTracks;
    final songMatchState = ref.watch(songMatchProvider);
    final showRecentSearchHistory = _isSearching &&
        _urlController.text.trim().isEmpty &&
        !isSearchLoading &&
        !searchPage.hasResults &&
        _recentSearchHistory.isNotEmpty;
    final songMatchLauncher = _buildSongMatchLauncher(songMatchState);
    final recentSearchHistorySection = RecentSearchHistorySection(
      queries: _recentSearchHistory,
      isLoading: _isRecentSearchHistoryLoading,
      onSelectQuery: (query) => _performSearch(ref, query),
    );
    final matchingPlaylists = _isSearching
        ? [
            ..._searchLocalPlaylists(playlists, _urlController.text),
            ..._searchDiscoveryPlaylists(searchPage, _urlController.text),
          ]
        : const <Map<String, dynamic>>[];
    final neatieHomeExperience = _buildNeatieHomeExperience(
      recState: recState,
      lastPlayedTracks: lastPlayedTracks,
      frequentTracks: frequentTracks,
      displayName: displayName,
      avatarUrl: avatarUrl,
      isLoading: isRecLoading,
    );

    if (widget.searchOnly) {
      ref.listen<SongMatchState>(songMatchProvider, (previous, next) {
        if (!mounted) return;
        if (next.presentationToken == _lastSongMatchPresentationToken) {
          return;
        }
        _lastSongMatchPresentationToken = next.presentationToken;
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (!mounted) return;
          if (next.phase == SongMatchPhase.resolved &&
              next.resolvedTrack != null) {
            unawaited(_openMatchedTrackActions(next.resolvedTrack!));
            return;
          }
          if (next.phase == SongMatchPhase.ambiguous &&
              next.alternatives.isNotEmpty) {
            unawaited(_showSongMatchAlternatives(next.alternatives));
            return;
          }
          if (next.phase == SongMatchPhase.failed &&
              next.errorMessage.trim().isNotEmpty) {
            final messenger = ScaffoldMessenger.maybeOf(context);
            messenger
              ?..hideCurrentSnackBar()
              ..showSnackBar(
                SnackBar(
                  content: Text(next.errorMessage.trim()),
                  behavior: SnackBarBehavior.floating,
                ),
              );
          }
        });
      });
    }

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _primeLikelyTracks(visibleTracks);
    });
    if (!widget.searchOnly) {
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
                  if (widget.searchOnly) const NeatieSearchMasthead(),
                  SearchHeaderBar(
                    controller: _urlController,
                    focusNode: _searchFocusNode,
                    showBackButton: !widget.searchOnly,
                    onBack: () => _clearSearch(refreshRecommendations: false),
                    onClear: () => _clearSearch(refreshRecommendations: false),
                    onSubmitted: (_) => _performSearch(ref),
                  ),
                  const SizedBox(height: 12),
                ],

                if (showSearchSuggestions)
                  SearchSuggestionPanel(
                    suggestions: suggestState,
                    onSelectSuggestion: (query) => _performSearch(ref, query),
                  ),

                SizedBox(height: showSearchSuggestions ? 4 : 20),

                // Content Area (Search vs Recommendations)
                if (_isSearching) ...[
                  if (_urlController.text.trim().isEmpty &&
                      !searchPage.hasResults &&
                      !isSearchLoading)
                    NeatieSearchLanding(
                      songMatchLauncher: songMatchLauncher,
                      recentSearchHistorySection: recentSearchHistorySection,
                      showRecentSearchHistory: showRecentSearchHistory,
                      onBrowseSelected: (surface) =>
                          _openBrowseSurface(ref, surface),
                    )
                  else ...[
                    NeatieSearchTabBar(
                      selected: _selectedSearchTab,
                      onSelected: (tab) =>
                          setState(() => _selectedSearchTab = tab),
                    ),
                    const SizedBox(height: 12),
                    NeatieSearchResultsSection(
                      query: _urlController.text.trim(),
                      selectedTab: _selectedSearchTab,
                      isLoading: isSearchLoading,
                      tracks: searchPage.tracks,
                      artists: searchPage.artists,
                      albums: displayAlbums,
                      similarArtists: searchPage.similarArtists,
                      artistWorks: [
                        ...searchPage.artistTracks,
                        ...searchPage.relatedAlbums,
                      ],
                      similarTracks: similarTracks,
                      playlists: matchingPlaylists,
                      errorMessage: searchPage.errorMessage,
                      onPlayTrack: (track) =>
                          unawaited(_playSearchDiscoveryTrack(track)),
                      onOpenTrack: (track) =>
                          _openTrackDetails(track, extractTrackId(track)),
                      onOpenArtist: (artist) => unawaited(_openArtist(artist)),
                      onOpenAlbum: (album) => unawaited(_openAlbum(album)),
                      onOpenPlaylist: (playlist) =>
                          unawaited(_openPlaylist(playlist)),
                    ),
                  ],
                ] else ...[
                  neatieHomeExperience,
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
