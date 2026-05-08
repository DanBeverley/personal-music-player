import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'home_screen_controller.dart';
import 'logic/audio_provider.dart';
import 'logic/audio_provider_history.dart';
import 'logic/audio_provider_queue.dart';
import 'logic/audio_provider_recommendation.dart';
import 'logic/details_provider.dart';
import 'logic/search_provider.dart';
import 'logic/song_match_provider.dart';
import 'main_details.dart';
import 'main_player.dart';
import 'screens/assistant_screen.dart';
import 'screens/personal_mix_detail_screen.dart';
import 'screens/recommendation_row_detail_screen.dart';
import 'ui/app_theme_tokens.dart';
import 'widgets/app_artwork.dart';
import 'widgets/home/home_collection_widgets.dart';
import 'widgets/home/genre_widgets.dart';
import 'widgets/home/home_content_sections.dart';
import 'widgets/home/recent_search_history_section.dart';
import 'widgets/home/search_surface_widgets.dart';
import 'widgets/home/song_match_widgets.dart';
import 'widgets/home/track_menu_button.dart';
import 'widgets/playlist/add_to_playlist_dialog.dart';
import 'widgets/track_list_skeleton.dart';

const _accentGrey = appAccentGrey;
const _surfaceGrey = appSurfaceGrey;
const _surfaceGreyAlt = appSurfaceGreyAlt;
const double _radiusLarge = appRadiusLarge;
const double _radiusMedium = appRadiusMedium;
const List<String> _quipOpeners = <String>[
  'Today\'s sonic forecast',
  'Current music goblin memo',
  'Breaking genre news',
  'The headphone council reports',
  'Fresh dispatch from the aux cord tribunal',
];
const List<String> _quipMiddles = <String>[
  'your queue is one impulsive click away from becoming a legal issue',
  'someone out there is still defending their 47-minute bagpipe ambient cut',
  'the algorithm briefly achieved sentience and immediately asked for one more chorus',
  'your taste is giving â€œI know a shortcutâ€\u009d right before getting lost in a concept album',
  'a bassline just kicked the door open and demanded better transitions',
];
const List<String> _quipClosers = <String>[
  'Proceed with swagger.',
  'Use responsibly near the skip button.',
  'No moderators were consulted.',
  'Respectfully: play it too loud.',
  'The aux has never been safer.',
];

class HomeScreen extends ConsumerStatefulWidget {
  final bool searchOnly;
  final HomeScreenController? controller;

  const HomeScreen({
    super.key,
    this.searchOnly = false,
    this.controller,
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
    for (final track in tracks.take(12)) {
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

    unawaited(
      ref.read(audioPlayerProvider.notifier).prewarmStreams(
            idsToWarm,
            lookahead: math.max(10, idsToWarm.length),
            immediatePlayback: true,
            currentVideoId: idsToWarm.first,
          ),
    );
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
    Map<String, dynamic> meta = const <String, dynamic>{},
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildRecommendationLaneHeader(
          title: title,
          statusLabel: _isRefiningFlagshipMeta(meta) ? 'Refining' : null,
        ),
        if (_isRefiningFlagshipMeta(meta))
          _buildPartialReadyFlagshipNote(meta)
        else
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
        _buildRecommendationLaneHeader(
          title: title,
          statusLabel: _isRefiningFlagshipMeta(meta) ? 'Refining' : null,
        ),
        if (_isRefiningFlagshipMeta(meta))
          _buildPartialReadyFlagshipNote(meta)
        else
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
                            for (var index = 0;
                                index < page.length;
                                index++) ...[
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
                        if (index != pages.length - 1) const SizedBox(width: 6),
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
    String? statusLabel,
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
        if (statusLabel != null && statusLabel.trim().isNotEmpty) ...[
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.05),
              borderRadius: BorderRadius.circular(999),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
              ),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                SizedBox(
                  width: 11,
                  height: 11,
                  child: CircularProgressIndicator(
                    strokeWidth: 1.7,
                    color: Colors.white.withValues(alpha: 0.72),
                  ),
                ),
                const SizedBox(width: 7),
                Text(
                  statusLabel.trim(),
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.74),
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.3,
                  ),
                ),
              ],
            ),
          ),
          if (onOpenDetail != null) const SizedBox(width: 10),
        ],
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

  Widget _buildPartialReadyFlagshipNote(Map<String, dynamic> meta) {
    if (!_isRefiningFlagshipMeta(meta)) {
      return const SizedBox.shrink();
    }
    final label = meta['loading_label']?.toString().trim().isNotEmpty == true
        ? meta['loading_label'].toString().trim()
        : 'Refining';
    final message = meta['loading_message']?.toString().trim().isNotEmpty == true
        ? meta['loading_message'].toString().trim()
        : 'This lane is still pulling in stronger picks.';
    return Padding(
      padding: const EdgeInsets.only(top: 10, bottom: 14),
      child: Row(
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.045),
              borderRadius: BorderRadius.circular(999),
              border: Border.all(
                color: Colors.white.withValues(alpha: 0.08),
              ),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                SizedBox(
                  width: 12,
                  height: 12,
                  child: CircularProgressIndicator(
                    strokeWidth: 1.8,
                    color: Colors.white.withValues(alpha: 0.72),
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  label,
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.76),
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.4,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              message,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.5),
                fontSize: 12,
                fontWeight: FontWeight.w500,
                height: 1.35,
              ),
            ),
          ),
        ],
      ),
    );
  }

  bool _isRefiningFlagshipMeta(Map<String, dynamic> meta) {
    return meta['refinement_active'] == true ||
        (meta['row_state']?.toString().trim() ?? '') == 'partial_inflight';
  }

  Widget _buildRecommendationLaneSkeleton({
    int count = 3,
    double height = 244,
    double cardWidth = 182,
  }) {
    return SizedBox(
      height: height,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: count,
        separatorBuilder: (_, __) => const SizedBox(width: 14),
        itemBuilder: (context, index) => Container(
          width: cardWidth,
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.035),
            borderRadius: BorderRadius.circular(_radiusLarge),
            border: Border.all(color: Colors.white.withValues(alpha: 0.05)),
          ),
        ),
      ),
    );
  }

  Widget _buildDeferredFlagshipHeader(
    String title, {
    required Map<String, dynamic> meta,
  }) {
    final loadingLabelRaw = meta['loading_label']?.toString().trim() ?? '';
    final loadingMessageRaw = meta['loading_message']?.toString().trim() ?? '';
    final loadingLabel =
        loadingLabelRaw.isEmpty ? 'Preparing' : loadingLabelRaw;
    final loadingMessage = loadingMessageRaw.isEmpty
        ? 'Refreshing this lane with more personalized picks.'
        : loadingMessageRaw;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildRecommendationLaneHeader(title: title),
        const SizedBox(height: 10),
        Row(
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.045),
                borderRadius: BorderRadius.circular(999),
                border: Border.all(
                  color: Colors.white.withValues(alpha: 0.08),
                ),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  SizedBox(
                    width: 12,
                    height: 12,
                    child: CircularProgressIndicator(
                      strokeWidth: 1.8,
                      color: Colors.white.withValues(alpha: 0.72),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Text(
                    loadingLabel,
                    style: TextStyle(
                      color: Colors.white.withValues(alpha: 0.76),
                      fontSize: 11,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.4,
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                loadingMessage,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.5),
                  fontSize: 12,
                  fontWeight: FontWeight.w500,
                  height: 1.35,
                ),
              ),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buildDeferredFlagshipLaneShell({
    required String rowKind,
    required String title,
    required String itemType,
    required String rowStyle,
    required Map<String, dynamic> meta,
  }) {
    if (itemType == 'artist') {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _buildDeferredFlagshipHeader(title, meta: meta),
          const SizedBox(height: 14),
          HomeArtistRow(
            artists: const [],
            title: title,
            showTitle: false,
            isLoading: true,
            onOpenArtist: (_) {},
          ),
        ],
      );
    }

    if (itemType == 'album') {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _buildDeferredFlagshipHeader(title, meta: meta),
          const SizedBox(height: 16),
          _buildRecommendationLaneSkeleton(
            count: 2,
            height: 258,
            cardWidth: 188,
          ),
        ],
      );
    }

    if (rowStyle == 'mix_cards' || itemType == 'mix') {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _buildDeferredFlagshipHeader(title, meta: meta),
          const SizedBox(height: 16),
          _buildRecommendationLaneSkeleton(
            count: 2,
            height: 226,
            cardWidth: 214,
          ),
        ],
      );
    }

    if (rowStyle == 'genre_tabs') {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _buildDeferredFlagshipHeader(title, meta: meta),
          const SizedBox(height: 16),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            physics: const NeverScrollableScrollPhysics(),
            child: Row(
              children: List<Widget>.generate(
                4,
                (index) => Container(
                  width: index == 0 ? 96 : 82,
                  height: 34,
                  margin: EdgeInsets.only(right: index == 3 ? 0 : 10),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.05),
                    borderRadius: BorderRadius.circular(999),
                    border: Border.all(
                      color: Colors.white.withValues(alpha: 0.08),
                    ),
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(height: 16),
          const TrackListSkeleton(count: 2),
        ],
      );
    }

    if (rowKind == 'quiet_picks') {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _buildDeferredFlagshipHeader(title, meta: meta),
          const SizedBox(height: 16),
          const TrackListSkeleton(count: 3),
        ],
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildDeferredFlagshipHeader(title, meta: meta),
        const SizedBox(height: 16),
        _buildRecommendationLaneSkeleton(),
      ],
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
    final isPendingFlagship = meta['deferred_flagship'] == true &&
        (meta['loading_state']?.toString() ?? '') == 'pending';
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

    if (items.isEmpty) {
      if (!isPendingFlagship) {
        return const SizedBox.shrink();
      }
      return AnimatedSwitcher(
        duration: const Duration(milliseconds: 320),
        switchInCurve: Curves.easeOutCubic,
        switchOutCurve: Curves.easeInCubic,
        transitionBuilder: (child, animation) => FadeTransition(
          opacity: animation,
          child: SlideTransition(
            position: Tween<Offset>(
              begin: const Offset(0, 0.05),
              end: Offset.zero,
            ).animate(animation),
            child: child,
          ),
        ),
        child: KeyedSubtree(
          key: ValueKey<String>('$rowId:pending'),
          child: _buildDeferredFlagshipLaneShell(
            rowKind: rowId,
            title: title,
            itemType: itemType,
            rowStyle: rowStyle,
            meta: meta,
          ),
        ),
      );
    }

    Widget lane;

    if (rowStyle == 'featured_track') {
      lane = _buildFeaturedTrackLane(
        title: title,
        items: items,
        meta: meta,
      );
    } else if (rowStyle == 'mix_cards' || itemType == 'mix') {
      lane = _buildMixCardsLane(
        title: title,
        items: items,
        meta: meta,
      );
    } else if (rowStyle == 'genre_tabs') {
      lane = _buildGenreTabsLane(
        rowId: rowId,
        title: title,
        fallbackItems: items,
        meta: meta,
        isUpdating: isPaginating,
      );
    } else if (itemType == 'album') {
      lane = _buildAlbumRecommendationLane(
        rowId: rowId,
        title: title,
        items: items,
        nextOffset: nextOffset,
        isPaginating: isPaginating,
        hasMore: hasMore,
        meta: meta,
      );
    } else if (itemType == 'artist') {
      lane = Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _buildRecommendationLaneHeader(
            title: title,
            statusLabel: _isRefiningFlagshipMeta(meta) ? 'Refining' : null,
          ),
          if (_isRefiningFlagshipMeta(meta))
            _buildPartialReadyFlagshipNote(meta)
          else
            const SizedBox(height: 14),
          HomeArtistRow(
            artists: items,
            title: title,
            showTitle: false,
            isLoading: isLoading,
            onOpenArtist: (artist) => unawaited(_openArtist(artist)),
          ),
        ],
      );
    } else if (rowId == 'quiet_picks') {
      lane = _buildVerticalRecommendationLane(
        rowId: rowId,
        title: title,
        items: items,
        nextOffset: nextOffset,
        isPaginating: isPaginating,
        hasMore: hasMore,
      );
    } else {
      lane = Column(
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

    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 320),
      switchInCurve: Curves.easeOutCubic,
      switchOutCurve: Curves.easeInCubic,
      transitionBuilder: (child, animation) => FadeTransition(
        opacity: animation,
        child: SlideTransition(
          position: Tween<Offset>(
            begin: const Offset(0, 0.05),
            end: Offset.zero,
          ).animate(animation),
          child: child,
        ),
      ),
      child: KeyedSubtree(
        key: ValueKey<String>(
          '$rowId:${meta['row_state'] ?? (isPendingFlagship ? 'pending' : 'ready')}:${meta['row_version'] ?? 0}:${items.length}:${hasMore ? 'more' : 'done'}',
        ),
        child: lane,
      ),
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
              : () =>
                  ref.read(recommendationProvider.notifier).loadMoreRow(rowId),
        ),
        if (hasMore || isPaginating)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Center(
              child: TextButton(
                onPressed: isPaginating
                    ? null
                    : () => ref
                        .read(recommendationProvider.notifier)
                        .loadMoreRow(rowId),
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

  Widget _buildAlbumRecommendationLane({
    required String rowId,
    required String title,
    required List<Map<String, dynamic>> items,
    required int nextOffset,
    required bool isPaginating,
    required bool hasMore,
    Map<String, dynamic> meta = const <String, dynamic>{},
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildRecommendationLaneHeader(
          title: title,
          statusLabel: _isRefiningFlagshipMeta(meta) ? 'Refining' : null,
        ),
        if (_isRefiningFlagshipMeta(meta))
          _buildPartialReadyFlagshipNote(meta)
        else
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
    unawaited(
      ref.read(audioPlayerProvider.notifier).prepareImmediatePlayback(videoId),
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

  // ignore: unused_element
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
        final nearEndIndex =
            math.max(0, tracks.length - math.max(1, nearEndLead));
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
    final searchTrackIds = searchPage.tracks
        .map((track) => extractTrackId(track) ?? '')
        .where((id) => id.trim().isNotEmpty)
        .toSet();
    final similarTracks = _isSearching
        ? searchPage.similarTracks
            .where((track) => !searchTrackIds.contains(extractTrackId(track)))
            .toList(growable: false)
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
    final featuredArtistCard = featuredArtist != null
        ? HomeFeaturedArtistCard(
            artist: featuredArtist,
            onOpenArtist: (artist) => unawaited(_openArtist(artist)),
          )
        : null;
    final artistMatchLoadingCard = featuredArtist == null && isSearchLoading
        ? Column(
            children: [
              HomeArtistRow(
                artists: const [],
                title: 'Artist match',
                isLoading: true,
                onOpenArtist: (artist) => unawaited(_openArtist(artist)),
              ),
              const SizedBox(height: 10),
            ],
          )
        : null;
    final similarArtistsSection = HomeArtistRow(
      artists: similarArtists,
      title: 'Similar artists',
      isLoading: isSearchLoading,
      onOpenArtist: (artist) => unawaited(_openArtist(artist)),
    );
    final featuredAlbumSection = featuredAlbum != null
        ? HomeAlbumList(
            albums: [featuredAlbum],
            isLoading: false,
            onOpenAlbum: (album) => unawaited(_openAlbum(album)),
          )
        : null;
    final relatedTracksSection = similarTracks.isNotEmpty
        ? Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Similar tracks',
                style: TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.bold,
                  color: Colors.white,
                ),
              ),
              const SizedBox(height: 12),
              HomeTrackList(
                tracks: similarTracks,
                isLoading: false,
                emptyMessage: '',
                playlistId:
                    'search-similar:${_urlController.text.trim().toLowerCase()}',
                playlistName: _urlController.text.trim().isEmpty
                    ? 'Similar tracks'
                    : 'Similar to ${_urlController.text.trim()}',
                playAsDiscoveryMix: true,
                onWarmTrack: _warmTrack,
                onPlayTrack: (
                  normalizedTracks,
                  track,
                  effectivePlaylistId,
                  effectivePlaylistName,
                ) =>
                    _playTrackFromCollection(
                  tracks: normalizedTracks,
                  track: track,
                  playlistId: effectivePlaylistId,
                  playlistName: effectivePlaylistName,
                ),
                onPlayDiscoveryTrack: (track, sessionName) =>
                    _playTrackAsDiscoveryMix(track, sessionName: sessionName),
                onOpenDetails: (track, videoId) =>
                    _openTrackDetails(track, videoId),
                onAddToPlaylist: _addTrackToPlaylistFromMenu,
                onStartStation: _startTrackStationFromMenu,
              ),
            ],
          )
        : null;
    final trackListSection = HomeTrackList(
      tracks: searchPage.tracks,
      isLoading: isSearchLoading,
      emptyMessage: searchPage.errorMessage?.trim().isNotEmpty == true
          ? searchPage.errorMessage!.trim()
          : 'No results found.',
      playlistId: 'search:${_urlController.text.trim().toLowerCase()}',
      playlistName: _urlController.text.trim().isEmpty
          ? 'Search results'
          : 'Search: ${_urlController.text.trim()}',
      playAsDiscoveryMix: true,
      onWarmTrack: _warmTrack,
      onPlayTrack: (
        normalizedTracks,
        track,
        effectivePlaylistId,
        effectivePlaylistName,
      ) =>
          _playTrackFromCollection(
        tracks: normalizedTracks,
        track: track,
        playlistId: effectivePlaylistId,
        playlistName: effectivePlaylistName,
      ),
      onPlayDiscoveryTrack: (track, sessionName) =>
          _playTrackAsDiscoveryMix(track, sessionName: sessionName),
      onOpenDetails: (track, videoId) => _openTrackDetails(track, videoId),
      onAddToPlaylist: _addTrackToPlaylistFromMenu,
      onStartStation: _startTrackStationFromMenu,
    );
    final moreAlbumsSection = extraAlbums.isNotEmpty || isSearchLoading
        ? Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'More albums',
                style: TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.bold,
                  color: Colors.white,
                ),
              ),
              const SizedBox(height: 16),
              HomeAlbumList(
                albums: extraAlbums,
                isLoading: isSearchLoading,
                onOpenAlbum: (album) => unawaited(_openAlbum(album)),
              ),
            ],
          )
        : null;
    final lastPlayedLane = lastPlayedTracks.isNotEmpty
        ? _buildRecommendationLane(
            rowId: 'last_played_local',
            title: 'Last played',
            itemType: 'track',
            items: lastPlayedTracks,
            nextOffset: lastPlayedTracks.length,
            isLoading: isLastPlayedLoading,
          )
        : null;
    final frequentlyListenedLane = frequentTracks.isNotEmpty
        ? _buildRecommendationLane(
            rowId: 'frequently_listened_local',
            title: 'Frequently listened',
            itemType: 'track',
            items: frequentTracks,
            nextOffset: frequentTracks.length,
            isLoading: isFrequentlyLoading,
          )
        : null;
    final recommendationRows = AnimatedSwitcher(
      duration: const Duration(milliseconds: 220),
      switchInCurve: Curves.easeOutCubic,
      switchOutCurve: Curves.easeInCubic,
      child: _buildRecommendationFeedRows(
        recState,
        isLoading: isRecLoading,
        hiddenKinds: (frequentTracks.isNotEmpty &&
                recState.rows.any(
                  (row) =>
                      row.kind != 'frequently_listened' && row.items.isNotEmpty,
                ))
            ? const {'frequently_listened'}
            : const {},
      ),
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

                if (!_isSearching && !showSearchSuggestions) ...[
                  const SizedBox(height: 8),
                  AssistantLauncherCard(
                    tooltip: _heroQuip,
                    onTap: _openAssistant,
                  ),
                ],

                SizedBox(height: showSearchSuggestions ? 4 : 20),

                // Content Area (Search vs Recommendations)
                if (_isSearching) ...[
                  SearchContentSection(
                    songMatchLauncher: songMatchLauncher,
                    showRecentSearchHistory: showRecentSearchHistory,
                    recentSearchHistorySection: recentSearchHistorySection,
                    featuredArtistCard: featuredArtistCard,
                    artistMatchLoadingCard: artistMatchLoadingCard,
                    showSimilarArtists:
                        similarArtists.isNotEmpty || isSearchLoading,
                    similarArtistsSection: similarArtistsSection,
                    featuredAlbumSection: featuredAlbumSection,
                    relatedTracksSection: relatedTracksSection,
                    trackListSection: trackListSection,
                    moreAlbumsSection: moreAlbumsSection,
                  ),
                ] else ...[
                  HomeRecommendationsSection(
                    lastPlayedLane: lastPlayedLane,
                    frequentlyListenedLane: frequentlyListenedLane,
                    recommendationRows: recommendationRows,
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
