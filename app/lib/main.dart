import 'dart:io';
import 'dart:ui';
import 'dart:convert';
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:http/http.dart' as http;
import 'logic/audio_provider.dart';
import 'logic/download_provider.dart';
import 'logic/playlist_provider.dart';
import 'widgets/app_artwork.dart';
import 'widgets/download_hud.dart';

const _accentGrey = Color(0xFFB7BBC4);
const _surfaceGrey = Color(0xFF141414);
const _surfaceGreyAlt = Color(0xFF1B1B1B);
const _voidBlack = Color(0xFF070707);

void showGlassDialog({required BuildContext context, required String title, required Widget content, required List<Widget> actions}) {
  showGeneralDialog(
    context: context,
    barrierDismissible: true,
    barrierLabel: MaterialLocalizations.of(context).modalBarrierDismissLabel,
    barrierColor: Colors.black54,
    transitionDuration: const Duration(milliseconds: 300),
    pageBuilder: (BuildContext buildContext, Animation<double> animation, Animation<double> secondaryAnimation) {
      return BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 10, sigmaY: 10),
        child: ScaleTransition(
          scale: CurvedAnimation(parent: animation, curve: Curves.easeOutBack),
          child: AlertDialog(
            backgroundColor: Colors.grey[900]?.withValues(alpha: 0.7),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(20),
              side: BorderSide(color: Colors.white.withValues(alpha: 0.1), width: 1),
            ),
            title: Text(title, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
            content: content,
            actions: actions,
          )
        )
      );
    }
  );
}

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initAudioService();
  runApp(const ProviderScope(child: AuralisApp()));
}

class AuralisApp extends StatelessWidget {
  const AuralisApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Auralis',
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: _voidBlack,
        primaryColor: _accentGrey,
        colorScheme: const ColorScheme.dark(
          primary: _accentGrey,
          secondary: _accentGrey,
          surface: _surfaceGrey,
          onSurface: Colors.white,
        ),
        textTheme: GoogleFonts.spaceGroteskTextTheme(Theme.of(context).textTheme)
            .apply(bodyColor: Colors.white, displayColor: Colors.white),
        appBarTheme: const AppBarTheme(
          backgroundColor: Colors.transparent,
          elevation: 0,
        ),
      ),
      home: const MainLayout(),
    );
  }
}

class MainLayout extends ConsumerStatefulWidget {
  const MainLayout({super.key});

  @override
  ConsumerState<MainLayout> createState() => _MainLayoutState();
}

class _MainLayoutState extends ConsumerState<MainLayout> {
  int _currentIndex = 0;

  final List<Widget> _pages = [
    const HomeScreen(),
    const LibraryScreen(),
  ];

  @override
  Widget build(BuildContext context) {
    final hasActiveTrack = ref.watch(
      audioPlayerProvider.select(
        (state) => state.currentTrackName != 'No track loaded',
      ),
    );

    return Scaffold(
      body: Container(
        decoration: const BoxDecoration(
          gradient: RadialGradient(
            center: Alignment.topCenter,
            radius: 1.2,
            colors: [Color(0xFF171717), _voidBlack, Colors.black],
          ),
        ),
        child: Stack(
          children: [
            FadeIndexedStack(
              index: _currentIndex,
              children: _pages,
            ),
            Positioned(
              right: 24,
              bottom: hasActiveTrack ? 170 : 96,
              child: const DownloadHud(),
            ),
            AnimatedPositioned(
              duration: const Duration(milliseconds: 300),
              curve: Curves.easeOutCubic,
              bottom: hasActiveTrack ? 88 : -100,
              left: 0,
              right: 0,
              child: const MiniPlayer(),
            ),
            Positioned(
              bottom: 24,
              left: 24,
              right: 24,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(36),
                child: BackdropFilter(
                  filter: ImageFilter.blur(sigmaX: 16, sigmaY: 16),
                  child: Container(
                    height: 68,
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.05),
                      borderRadius: BorderRadius.circular(36),
                      border: Border.all(color: Colors.white.withValues(alpha: 0.08), width: 1),
                    ),
                    child: Row(
                      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                      children: [
                        _buildNavItem(Icons.explore_outlined, Icons.explore, 'Listen', 0),
                        _buildNavItem(Icons.queue_music_outlined, Icons.queue_music, 'Library', 1),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildNavItem(IconData unselected, IconData selected, String label, int index) {
    final isSelected = _currentIndex == index;
    return GestureDetector(
      onTap: () => setState(() => _currentIndex = index),
      behavior: HitTestBehavior.opaque,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          AnimatedContainer(
            duration: const Duration(milliseconds: 300),
            curve: Curves.easeOutBack,
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            decoration: BoxDecoration(
              color: isSelected ? Colors.white.withValues(alpha: 0.15) : Colors.transparent,
              borderRadius: BorderRadius.circular(24),
            ),
            child: Icon(
              isSelected ? selected : unselected,
              color: isSelected ? Colors.white : Colors.white54,
              size: 26,
            ),
          ),
          if (isSelected) 
             Container(
               margin: const EdgeInsets.only(top: 4),
               width: 4, height: 4, 
               decoration: const BoxDecoration(shape: BoxShape.circle, color: Colors.white)
             )
        ],
      ),
    );
  }
}

class HomeScreen extends ConsumerStatefulWidget {
  const HomeScreen({super.key});

  @override
  ConsumerState<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends ConsumerState<HomeScreen> {
  final TextEditingController _urlController = TextEditingController();
  final FocusNode _searchFocusNode = FocusNode();
  bool _isSearching = false;
  Timer? _suggestDebounce;
  final Set<String> _prewarmedTrackIds = <String>{};

  @override
  void initState() {
    super.initState();
    _urlController.addListener(() {
      final text = _urlController.text.trim();
      if (text.isNotEmpty && !_isSearching) {
        _suggestDebounce?.cancel();
        _suggestDebounce = Timer(const Duration(milliseconds: 280), () {
          if (mounted) {
            ref.read(suggestProvider.notifier).fetchSuggestions(text);
          }
        });
      } else if (text.isEmpty) {
        _suggestDebounce?.cancel();
        ref.read(suggestProvider.notifier).clear();
        setState(() => _isSearching = false);
      }
    });
  }

  @override
  void dispose() {
    _suggestDebounce?.cancel();
    _urlController.dispose();
    _searchFocusNode.dispose();
    super.dispose();
  }

  Future<void> _performSearch(WidgetRef ref, [String? query]) async {
    final q = query ?? _urlController.text.trim();
    if (q.isEmpty) {
      setState(() => _isSearching = false);
      return;
    }
    _urlController.text = q;
    _searchFocusNode.unfocus();
    setState(() => _isSearching = true);
    ref.read(suggestProvider.notifier).clear();
    await ref.read(searchProvider.notifier).search(q);
  }

  void _clearSearch() {
    _urlController.clear();
    _searchFocusNode.unfocus();
    ref.read(suggestProvider.notifier).clear();
    setState(() => _isSearching = false);
  }

  void _primeLikelyTracks(List<dynamic> tracks) {
    for (final track in tracks.take(3)) {
      final id = (track['id'] ?? track['videoId'])?.toString();
      if (id == null || id.isEmpty) continue;
      if (_prewarmedTrackIds.add(id)) {
        unawaited(ref.read(audioPlayerProvider.notifier).prewarmStream(id));
      }
    }
  }

  Future<void> _triggerStream(dynamic track) async {
    ref
        .read(audioPlayerProvider.notifier)
        .streamYoutube(track['id'] ?? track['videoId'], track);
        
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
  }

  Widget _buildTrackList(
      List<dynamic> tracks, bool isLoading, String emptyMessage) {
    if (isLoading && tracks.isEmpty) {
      return const Center(
          child: CircularProgressIndicator(color: _accentGrey));
    }
    if (tracks.isEmpty) {
      return Center(
        child:
            Text(emptyMessage, style: const TextStyle(color: Colors.white54)),
      );
    }
    return ListView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: tracks.length + (isLoading ? 1 : 0),
      itemBuilder: (context, index) {
        if (index >= tracks.length) {
          return const Center(
              child: Padding(
                  padding: EdgeInsets.all(16.0),
                  child: CircularProgressIndicator(color: _accentGrey)));
        }
        final t = tracks[index];
        final videoId = (t['id'] ?? t['videoId'])?.toString();
        return Container(
          margin: const EdgeInsets.only(bottom: 14),
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.025),
            borderRadius: BorderRadius.circular(24),
            border: Border.all(color: Colors.white.withValues(alpha: 0.06), width: 1),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.2),
                blurRadius: 18,
                offset: const Offset(0, 10),
              )
            ]
          ),
          child: Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(16),
              onTap: () => _triggerStream(t),
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
                      child: GestureDetector(
                        onTap: () {
                          ref.read(trackDetailsProvider.notifier).fetchDetails(t['id']);
                          Navigator.push(
                              context,
                              MaterialPageRoute(
                                  builder: (_) => TrackDetailsScreen(track: t)));
                        },
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(t['title'] ?? 'Unknown',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                    color: Colors.white, fontSize: 16, fontWeight: FontWeight.w700)),
                            const SizedBox(height: 4),
                            Text(t['channel'] ?? '',
                                maxLines: 1, style: TextStyle(color: Colors.white.withValues(alpha: 0.6), fontSize: 13)),
                          ],
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
                                      value: task!.progress > 0 ? task.progress : null,
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
    final searchState = ref.watch(searchProvider);
    final isSearchLoading = ref.watch(searchProvider.notifier).isLoading;

    final recState = ref.watch(recommendationProvider);
    final isRecLoading = ref.watch(recommendationProvider.notifier).isLoading;
    final suggestState = ref.watch(suggestProvider);
    final visibleTracks = _isSearching ? searchState : recState;

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _primeLikelyTracks(visibleTracks);
    });

    return SafeArea(
        child: NotificationListener<ScrollNotification>(
      onNotification: (ScrollNotification scrollInfo) {
        if (!_isSearching &&
            !isRecLoading &&
            scrollInfo.metrics.pixels >=
                scrollInfo.metrics.maxScrollExtent - 200) {
          // Pick a seed from existing recommendations to continuously fetch infinite scroll
          if (recState.isNotEmpty) {
            final lastTrackId = recState.last['id'];
            ref.read(recommendationProvider.notifier).loadMore(lastTrackId);
          }
        }
        return false;
      },
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(16.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Auralis',
              style: GoogleFonts.spaceGrotesk(
                fontSize: 32,
                fontWeight: FontWeight.w700,
                color: Colors.white,
                letterSpacing: -1,
              ),
            ),
            const SizedBox(height: 6),
            Text(
              'Pull songs straight into a quieter, cleaner library.',
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.62),
                fontSize: 14,
                fontWeight: FontWeight.w500,
              ),
            ),
            const SizedBox(height: 28),

            TextField(
              controller: _urlController,
              focusNode: _searchFocusNode,
              style: const TextStyle(color: Colors.white),
              decoration: InputDecoration(
                hintText: 'Search songs or paste URL...',
                hintStyle: const TextStyle(color: Colors.white54),
                filled: true,
                fillColor: _surfaceGrey,
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(22),
                  borderSide: BorderSide.none,
                ),
                prefixIcon: _isSearching || _urlController.text.isNotEmpty
                    ? IconButton(
                        icon: const Icon(Icons.arrow_back, color: Colors.white),
                        onPressed: _clearSearch,
                      )
                    : const Icon(Icons.search, color: Colors.white54),
                suffixIcon: IconButton(
                  icon: const Icon(Icons.search, color: Colors.white),
                  onPressed: () => _performSearch(ref),
                ),
              ),
              onSubmitted: (_) => _performSearch(ref),
            ),

            // Suggestions Dropdown
            if (suggestState.isNotEmpty && !_isSearching)
              Container(
                margin: const EdgeInsets.only(top: 4),
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
                      leading: const Icon(Icons.search, color: Colors.white54),
                      title: Text(suggestState[i],
                          style: const TextStyle(color: Colors.white)),
                      onTap: () => _performSearch(ref, suggestState[i]),
                    );
                  },
                ),
              ),

            const SizedBox(height: 20),

            // Content Area (Search vs Recommendations)
            if (_isSearching) ...[
              const Text('Search Results',
                  style: TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.bold,
                      color: Colors.white)),
              const SizedBox(height: 16),
              _buildTrackList(
                  searchState, isSearchLoading, "No results found."),
            ] else ...[
              const Text('Quiet picks',
                  style: TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.bold,
                      color: Colors.white)),
              const SizedBox(height: 16),
              _buildTrackList(
                  recState, isRecLoading, "Loading recommendations..."),
            ],
            const SizedBox(height: 80), // Padding for miniplayer
          ],
        ),
      ),
    ));
  }
}

class LibraryScreen extends ConsumerWidget {
  const LibraryScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final libraryAsync = ref.watch(libraryProvider);
    final playlists = ref.watch(playlistProvider);

    return SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                const Text('Playlists', style: TextStyle(color: Colors.white, fontSize: 24, fontWeight: FontWeight.bold)),
                TextButton.icon(
                  onPressed: () {
                    final ctrl = TextEditingController();
                    showGlassDialog(
                      context: context,
                      title: 'New Playlist',
                      content: TextField(
                        controller: ctrl,
                        style: const TextStyle(color: Colors.white),
                        decoration: const InputDecoration(hintText: 'Playlist Name', hintStyle: TextStyle(color: Colors.white54)),
                      ),
                      actions: [
                        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel', style: TextStyle(color: Colors.white54))),
                        TextButton(onPressed: () {
                          if (ctrl.text.trim().isNotEmpty) {
                            ref.read(playlistProvider.notifier).createPlaylist(ctrl.text.trim());
                          }
                          Navigator.pop(context);
                        }, child: const Text('Create', style: TextStyle(color: Colors.white)))
                      ]
                    );
                  }, 
                  icon: const Icon(Icons.add, color: Colors.white), 
                  label: const Text('Create', style: TextStyle(color: Colors.white))
                )
              ],
            ),
            const SizedBox(height: 16),
            if (playlists.isEmpty)
              const Text('No playlists yet. Create one to get started!', style: TextStyle(color: Colors.white54, fontSize: 16))
            else
              ListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                itemCount: playlists.length,
                itemBuilder: (context, index) {
                  final p = playlists[index];
                  return Container(
                    margin: const EdgeInsets.only(bottom: 12),
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.03),
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(color: Colors.white.withValues(alpha: 0.05), width: 1),
                      boxShadow: [
                        BoxShadow(color: Colors.black.withValues(alpha: 0.2), blurRadius: 10, offset: const Offset(0, 4))
                      ]
                    ),
                    child: ListTile(
                      onTap: () => Navigator.push(context, MaterialPageRoute(builder: (_) => PlaylistDetailScreen(playlistId: p.id))),
                      leading: Container(
                        width: 50, height: 50,
                        decoration: BoxDecoration(color: Colors.grey[800], borderRadius: BorderRadius.circular(8)),
                        child: const Icon(Icons.music_note, color: Colors.white54),
                      ),
                      title: Text(p.name, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
                      subtitle: Text('${p.tracks.length} tracks', style: const TextStyle(color: Colors.white54)),
                      trailing: PopupMenuButton<String>(
                        icon: const Icon(Icons.more_vert, color: Colors.white70),
                        color: Colors.grey[900],
                        onSelected: (val) {
                          if (val == 'delete') {
                            ref.read(playlistProvider.notifier).deletePlaylist(p.id);
                          } else if (val == 'rename') {
                            final ctrl = TextEditingController(text: p.name);
                            showGlassDialog(
                              context: context,
                              title: 'Rename Playlist',
                              content: TextField(
                                controller: ctrl,
                                style: const TextStyle(color: Colors.white),
                              ),
                              actions: [
                                TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel', style: TextStyle(color: Colors.white54))),
                                TextButton(onPressed: () {
                                  if (ctrl.text.trim().isNotEmpty) {
                                    ref.read(playlistProvider.notifier).renamePlaylist(p.id, ctrl.text.trim());
                                  }
                                  Navigator.pop(context);
                                }, child: const Text('Save', style: TextStyle(color: Colors.white)))
                              ]
                            );
                          }
                        },
                        itemBuilder: (BuildContext context) => [
                          const PopupMenuItem(value: 'rename', child: Text('Rename', style: TextStyle(color: Colors.white))),
                          const PopupMenuItem(value: 'delete', child: Text('Delete', style: TextStyle(color: Colors.red))),
                        ],
                      ),
                    ),
                  );
                },
              ),
            
            const SizedBox(height: 32),
            const Text('Your Offline Library', style: TextStyle(color: Colors.white, fontSize: 24, fontWeight: FontWeight.bold)),
            const SizedBox(height: 16),
            
            libraryAsync.when(
              data: (files) {
                if (files.isEmpty) {
                  return const Text('Your Offline Library is empty.', style: TextStyle(color: Colors.white54, fontSize: 16));
                }
                return ListView.builder(
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  itemCount: files.length,
                  itemBuilder: (context, index) {
                    final track = files[index];
                    final name = track['title'] ?? 'Unknown';
                    final path = track['local_path'];
                    final videoId =
                        (track['video_id'] ?? track['id'])?.toString();
                    return Container(
                      margin: const EdgeInsets.only(bottom: 12),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.03),
                        borderRadius: BorderRadius.circular(16),
                        border: Border.all(color: Colors.white.withValues(alpha: 0.05), width: 1),
                        boxShadow: [BoxShadow(color: Colors.black.withValues(alpha: 0.2), blurRadius: 10, offset: const Offset(0, 4))]
                      ),
                      child: Material(
                        color: Colors.transparent,
                        child: InkWell(
                          borderRadius: BorderRadius.circular(16),
                          onTap: () {
                            final audioNotifier =
                                ref.read(audioPlayerProvider.notifier);
                            final loaded = track['thumbnail'] != null
                                ? audioNotifier.loadLocalWithMeta(path, track)
                                : audioNotifier.loadLocalFile(path, name);
                            if (!loaded) {
                              ScaffoldMessenger.of(context).showSnackBar(
                                const SnackBar(
                                  content: Text(
                                      'This downloaded track could not be loaded for playback.'),
                                  duration: Duration(seconds: 2),
                                ),
                              );
                              return;
                            }
                            audioNotifier.play();
                            Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
                          },
                          child: Padding(
                            padding: const EdgeInsets.all(12.0),
                            child: Row(
                              children: [
                                AppArtwork(
                                  thumbnail: track['thumbnail'],
                                  videoId: videoId,
                                  width: 70,
                                  height: 70,
                                  radius: 16,
                                ),
                                const SizedBox(width: 16),
                                Expanded(
                                  child: Column(
                                    crossAxisAlignment: CrossAxisAlignment.start,
                                    children: [
                                      Text(name, maxLines: 1, overflow: TextOverflow.ellipsis,
                                          style: const TextStyle(color: Colors.white, fontSize: 16, fontWeight: FontWeight.w700)),
                                      const SizedBox(height: 4),
                                      Text(track['author'] ?? track['artist'] ?? '', maxLines: 1, style: TextStyle(color: Colors.white.withValues(alpha: 0.6), fontSize: 13)),
                                    ],
                                  ),
                                ),
                                Column(
                                  crossAxisAlignment: CrossAxisAlignment.end,
                                  mainAxisAlignment: MainAxisAlignment.center,
                                  children: [
                                    IconButton(
                                      icon: Container(
                                         padding: const EdgeInsets.all(6),
                                         decoration: BoxDecoration(color: Colors.redAccent.withValues(alpha: 0.1), shape: BoxShape.circle),
                                         child: const Icon(Icons.delete_outline_rounded, color: Colors.redAccent, size: 20)
                                      ),
                                      onPressed: () {
                                        try {
                                          File(path).deleteSync();
                                          final jsonPath = path.replaceAll('.mp3', '.json');
                                          if (File(jsonPath).existsSync()) File(jsonPath).deleteSync();
                                          ref.invalidate(libraryProvider);
                                        } catch (e) {
                                          // Ignored mapping
                                        }
                                      },
                                    ),
                                    if (track['filesize'] != null && track['filesize'] > 0)
                                      Padding(
                                        padding: const EdgeInsets.only(right: 8.0),
                                        child: Text('${(track['filesize'] / 1024 / 1024).toStringAsFixed(1)} MB',
                                          style: TextStyle(color: Colors.white.withValues(alpha: 0.3), fontSize: 10, fontWeight: FontWeight.w600),
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
                  },
                );
              },
              loading: () => const Center(child: CircularProgressIndicator(color: Colors.white)),
              error: (err, stack) => Center(child: Text('Error: $err', style: const TextStyle(color: Colors.red))),
            ),
            const SizedBox(height: 80),
          ],
        ),
      ),
    );
  }
}

class PlaylistDetailScreen extends ConsumerStatefulWidget {
  final String playlistId;
  const PlaylistDetailScreen({super.key, required this.playlistId});
  @override
  ConsumerState<PlaylistDetailScreen> createState() => _PlaylistDetailScreenState();
}

class _PlaylistDetailScreenState extends ConsumerState<PlaylistDetailScreen> {
  final _searchCtrl = TextEditingController();
  List<dynamic> _searchResults = [];
  bool _isSearching = false;

  Future<void> _search(String q) async {
    if (q.isEmpty) return;
    setState(() => _isSearching = true);
    try {
      final res = await http.post(Uri.parse('$proxyBaseUrl/search'), headers: {'Content-Type': 'application/json'}, body: jsonEncode({"query": q}));
      if (res.statusCode == 200) {
        setState(() => _searchResults = jsonDecode(res.body)['results'] ?? []);
      }
    } catch (e) {
      // Ignored
    }
    setState(() => _isSearching = false);
  }

  @override
  Widget build(BuildContext context) {
    final playlists = ref.watch(playlistProvider);
    final playlist = playlists.firstWhere((p) => p.id == widget.playlistId, orElse: () => Playlist(id: '', name: 'Deleted Playlist', tracks: []));

    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(backgroundColor: Colors.transparent, title: Text(playlist.name, style: const TextStyle(color: Colors.white))),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: _searchCtrl,
              style: const TextStyle(color: Colors.white),
              decoration: InputDecoration(
                hintText: "Let's find something for your playlist",
                hintStyle: const TextStyle(color: Colors.white54),
                filled: true,
                fillColor: _surfaceGrey,
                suffixIcon: IconButton(icon: const Icon(Icons.search, color: Colors.white), onPressed: () => _search(_searchCtrl.text)),
              ),
              onSubmitted: _search,
            ),
            const SizedBox(height: 16),
            if (_isSearching) const Center(child: CircularProgressIndicator(color: Colors.white)),
            if (_searchResults.isNotEmpty) ...[
              const Text('Search Results', style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.bold)),
              const SizedBox(height: 8),
              ListView.builder(
                 shrinkWrap: true,
                 physics: const NeverScrollableScrollPhysics(),
                 itemCount: _searchResults.length,
                 itemBuilder: (context, i) {
                   final t = _searchResults[i];
                   final videoId = (t['id'] ?? t['videoId'])?.toString();
                   return ListTile(
                     leading: AppArtwork(
                       thumbnail: t['thumbnail'],
                       videoId: videoId,
                       width: 50,
                       height: 50,
                       radius: 14,
                     ),
                     title: Text(t['title'] ?? 'Unknown', style: const TextStyle(color: Colors.white), maxLines: 1),
                     subtitle: Text(t['channel'] ?? '', style: const TextStyle(color: Colors.white54)),
                     trailing: IconButton(
                       icon: const Icon(Icons.add_circle_outline, color: Colors.white),
                       onPressed: () {
                         ref.read(playlistProvider.notifier).addTrackToPlaylist(widget.playlistId, t);
                         ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Added to ${playlist.name}'), duration: const Duration(seconds: 1)));
                       }
                     )
                   );
                 }
              ),
              const Divider(color: Colors.white24, height: 32),
            ],
            
            const Text('Playlist Tracks', style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            if (playlist.tracks.isEmpty) const Text('No tracks added yet.', style: TextStyle(color: Colors.white54)),
            ListView.builder(
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              itemCount: playlist.tracks.length,
              itemBuilder: (context, i) {
                 final t = playlist.tracks[i];
                 final videoId = (t['id'] ?? t['videoId'])?.toString();
                 return ListTile(
                    onTap: () {
                        ref.read(audioPlayerProvider.notifier).streamYoutube(t['id'] ?? t['videoId'], t);
                        Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
                    },
                    leading: AppArtwork(
                      thumbnail: t['thumbnail'],
                      videoId: videoId,
                      width: 50,
                      height: 50,
                      radius: 14,
                    ),
                    title: Text(t['title'] ?? 'Unknown', style: const TextStyle(color: Colors.white), maxLines: 1),
                    subtitle: Text(t['channel'] ?? t['author'] ?? '', style: const TextStyle(color: Colors.white54)),
                    trailing: PopupMenuButton<String>(
                      icon: const Icon(Icons.more_vert, color: Colors.white70),
                      color: Colors.grey[900],
                      onSelected: (val) {
                        if (val == 'remove') {
                          ref.read(playlistProvider.notifier).removeTrackFromPlaylist(widget.playlistId, t['id'] ?? t['videoId']);
                        }
                      },
                      itemBuilder: (_) => [const PopupMenuItem(value: 'remove', child: Text('Remove from Playlist', style: TextStyle(color: Colors.red)))]
                    )
                 );
              }
            )
          ]
        )
      )
    );
  }
}

class MiniPlayer extends ConsumerWidget {
  const MiniPlayer({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final playerState = ref.watch(audioPlayerProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final videoId = playerState.videoId;

    return GestureDetector(
      onTap: () {
        Navigator.of(context)
            .push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
      },
      child: ClipRRect(
        borderRadius: BorderRadius.circular(20),
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 15, sigmaY: 15),
          child: Container(
            height: 78,
            margin: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.06),
              borderRadius: BorderRadius.circular(24),
              border: Border.all(color: Colors.white.withValues(alpha: 0.08), width: 1),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.25),
                  blurRadius: 24,
                  offset: const Offset(0, 12),
                )
              ]
            ),
            child: Column(
              children: [
                Expanded(
                  child: Row(
                    children: [
                      const SizedBox(width: 8),
                      AppArtwork(
                        thumbnail: playerState.thumbnail,
                        videoId: videoId,
                        width: 54,
                        height: 54,
                        radius: 16,
                        heroTag: 'album_art_${playerState.currentTrackName}',
                      ),
                      const SizedBox(width: 14),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                            Text(
                              playerState.currentTrackName,
                              style: const TextStyle(
                                  color: Colors.white, fontWeight: FontWeight.w700, fontSize: 15),
                              overflow: TextOverflow.ellipsis,
                            ),
                            const SizedBox(height: 2),
                            if (playerState.duration > 0 || playerState.artist != null)
                              Text(
                                  playerState.artist ?? '${(playerState.currentPosition / 60).floor()}:${(playerState.currentPosition % 60).toString().padLeft(2, '0')} / ${(playerState.duration / 60).floor()}:${(playerState.duration % 60).toString().padLeft(2, '0')}',
                                  style: TextStyle(
                                      color: Colors.white.withValues(alpha: 0.6), fontSize: 12, fontWeight: FontWeight.w500)),
                          ],
                        ),
                      ),
                      // Play/Pause Button
                      GestureDetector(
                        onTap: () {
                          playerState.isPlaying
                            ? audioNotifier.pause()
                            : audioNotifier.play();
                        },
                        child: AnimatedContainer(
                          duration: const Duration(milliseconds: 220),
                          margin: const EdgeInsets.only(right: 12),
                          padding: const EdgeInsets.all(10),
                          decoration: BoxDecoration(
                             color: playerState.isPlaying
                                 ? Colors.white.withValues(alpha: 0.16)
                                 : Colors.white.withValues(alpha: 0.08),
                             shape: BoxShape.circle,
                          ),
                          child: playerState.isDownloading
                              ? const SizedBox(
                                  width: 28,
                                  height: 28,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                    color: Colors.white,
                                  ),
                                )
                              : Icon(
                                  playerState.isPlaying
                                      ? Icons.pause_rounded
                                      : Icons.play_arrow_rounded,
                                  color: Colors.white,
                                  size: 28,
                                ),
                        ),
                      ),
                    ],
                  ),
                ),
                // Progress Bar
                if (playerState.duration > 0)
                  ClipRRect(
                    borderRadius: const BorderRadius.only(bottomLeft: Radius.circular(20), bottomRight: Radius.circular(20)),
                    child: LinearProgressIndicator(
                      value: (playerState.currentPosition / playerState.duration)
                          .clamp(0.0, 1.0),
                      backgroundColor: Colors.transparent,
                      color: _accentGrey,
                      minHeight: 2,
                    ),
                  )
              ],
            ),
          ),
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
  double? _dragValue;

  @override
  Widget build(BuildContext context) {
    final playerState = ref.watch(audioPlayerProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    const accentColor = _accentGrey;
    final artworkUrl =
        preferredArtworkUrl(playerState.thumbnail, videoId: playerState.videoId);

    return Scaffold(
      extendBodyBehindAppBar: true,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.keyboard_arrow_down, color: Colors.white),
          onPressed: () => Navigator.pop(context),
        ),
        title: const Text('Now Playing',
            style: TextStyle(fontSize: 11, color: Colors.white, fontWeight: FontWeight.w700, letterSpacing: 1.4)),
        centerTitle: true,
      ),
      body: Stack(
        fit: StackFit.expand,
        children: [
          Container(color: _voidBlack),
          if (artworkUrl.isNotEmpty)
            Opacity(
              opacity: 0.16,
              child: Image.network(artworkUrl, fit: BoxFit.cover),
            ),
          BackdropFilter(
            filter: ImageFilter.blur(sigmaX: 60, sigmaY: 60),
            child: Container(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  colors: [
                    Colors.black.withValues(alpha: 0.2),
                    Colors.black.withValues(alpha: 0.74),
                    Colors.black.withValues(alpha: 0.98),
                  ],
                ),
              ),
            ),
          ),

          // Foreground Content
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32.0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                Center(
                  child: Hero(
                    tag: 'album_art_${playerState.currentTrackName}',
                    child: Container(
                      padding: const EdgeInsets.all(14),
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(36),
                        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
                        color: Colors.white.withValues(alpha: 0.03),
                        boxShadow: [
                          BoxShadow(
                            color: Colors.black.withValues(alpha: 0.44),
                            blurRadius: 48,
                            offset: const Offset(0, 26),
                          )
                        ],
                      ),
                      child: AppArtwork(
                        thumbnail: playerState.thumbnail,
                        videoId: playerState.videoId,
                        width: MediaQuery.of(context).size.width * 0.8,
                        height: MediaQuery.of(context).size.width * 0.8,
                        radius: 28,
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 60),
                Text(
                  playerState.currentTrackName,
                  style: const TextStyle(
                      fontSize: 28,
                      fontWeight: FontWeight.w800,
                      color: Colors.white,
                      height: 1.2),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 8),
                Text(
                  playerState.artist ?? 'Artist Unknown',
                  style: TextStyle(fontSize: 18, color: Colors.white.withValues(alpha: 0.7)),
                ),
                const SizedBox(height: 32),
                
                // Progress Bar
                SliderTheme(
                  data: SliderTheme.of(context).copyWith(
                    activeTrackColor: Colors.white,
                    inactiveTrackColor: Colors.white.withValues(alpha: 0.2),
                    thumbColor: Colors.white,
                    overlayColor: Colors.white.withValues(alpha: 0.1),
                    trackHeight: 4.0,
                    thumbShape: const RoundSliderThumbShape(enabledThumbRadius: 6),
                  ),
                  child: Slider(
                    value: playerState.duration > 0
                        ? _dragValue ??
                            (playerState.currentPosition / playerState.duration).clamp(0.0, 1.0)
                        : 0.0,
                    onChanged: playerState.duration > 0
                        ? (val) => setState(() => _dragValue = val)
                        : null,
                    onChangeEnd: playerState.duration > 0
                        ? (val) {
                            audioNotifier.seek((val * playerState.duration).toInt());
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
                          style: TextStyle(color: Colors.white.withValues(alpha: 0.6), fontSize: 12, fontWeight: FontWeight.w600)),
                      Text(
                          '${(playerState.duration / 60).floor()}:${(playerState.duration % 60).toString().padLeft(2, '0')}',
                          style: TextStyle(color: Colors.white.withValues(alpha: 0.6), fontSize: 12, fontWeight: FontWeight.w600)),
                    ],
                  ),
                ),
                const SizedBox(height: 24),
                
                // Controls
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                  children: [
                    IconButton(
                      icon: Icon(Icons.loop,
                          color: playerState.isLooping ? accentColor : Colors.white.withValues(alpha: 0.5)),
                      onPressed: () {
                        audioNotifier.toggleLoop(0, playerState.duration * 1000);
                      },
                    ),
                    Icon(Icons.skip_previous_rounded, color: Colors.white.withValues(alpha: 0.8), size: 48),
                    GestureDetector(
                      onTap: () {
                        playerState.isPlaying
                            ? audioNotifier.pause()
                            : audioNotifier.play();
                      },
                      child: Container(
                        decoration: const BoxDecoration(
                          shape: BoxShape.circle,
                          color: _surfaceGreyAlt,
                        ),
                        padding: const EdgeInsets.all(20),
                        child: playerState.isDownloading 
                           ? const SizedBox(width: 40, height: 40, child: CircularProgressIndicator(color: Colors.white, strokeWidth: 3))
                           : Icon(
                              playerState.isPlaying ? Icons.pause_rounded : Icons.play_arrow_rounded,
                              color: Colors.white,
                              size: 40),
                      ),
                    ),
                    Icon(Icons.skip_next_rounded, color: Colors.white.withValues(alpha: 0.8), size: 48),
                    IconButton(
                      icon: Icon(Icons.playlist_add, color: Colors.white.withValues(alpha: 0.5)),
                      onPressed: () {
                        if (playerState.videoId == null) return;
                        final trackMap = {
                           'id': playerState.videoId,
                           'title': playerState.currentTrackName,
                           'thumbnail': playerState.thumbnail,
                           'channel': playerState.artist,
                        };
                        final playlists = ref.read(playlistProvider);
                        showGlassDialog(
                          context: context,
                          title: 'Add to Playlist',
                          content: SizedBox(
                            width: double.maxFinite,
                            child: playlists.isEmpty 
                              ? const Text('No playlists created yet.', style: TextStyle(color: Colors.white54))
                              : ListView.builder(
                                  shrinkWrap: true,
                                  itemCount: playlists.length,
                                  itemBuilder: (context, i) => ListTile(
                                    title: Text(playlists[i].name, style: const TextStyle(color: Colors.white)),
                                    onTap: () {
                                      ref.read(playlistProvider.notifier).addTrackToPlaylist(playlists[i].id, trackMap);
                                      Navigator.pop(context);
                                      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Added to ${playlists[i].name}'), duration: const Duration(seconds: 1)));
                                    },
                                  )
                                )
                          ),
                          actions: [
                            TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel', style: TextStyle(color: Colors.white54)))
                          ]
                        );
                      }
                    ),
                  ],
                ),
                const SizedBox(height: 60),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class TrackDetailsScreen extends ConsumerWidget {
  final Map<String, dynamic> track;
  const TrackDetailsScreen({super.key, required this.track});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final details = ref.watch(trackDetailsProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    final videoId = (track['id'] ?? track['videoId'])?.toString();
    final downloadTask = videoId == null
        ? null
        : ref.watch(downloadTaskProvider(videoId));

    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        title: const Text(''),
        backgroundColor: Colors.transparent,
        elevation: 0,
      ),
      body: SingleChildScrollView(
        child: Column(
          children: [
            // Top Section: Track Info
            Container(
              padding: const EdgeInsets.all(24),
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topCenter,
                  end: Alignment.bottomCenter,
                  colors: [Colors.grey.shade900, Colors.black],
                ),
              ),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  AppArtwork(
                    thumbnail: track['thumbnail'],
                    videoId: videoId,
                    width: 140,
                    height: 140,
                    radius: 18,
                  ),
                  const SizedBox(width: 24),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('Song',
                            style: TextStyle(
                                color: Colors.white,
                                fontSize: 12,
                                fontWeight: FontWeight.bold)),
                        const SizedBox(height: 8),
                        Text(track['title'] ?? 'Unknown',
                            style: const TextStyle(
                                color: Colors.white,
                                fontSize: 32,
                                fontWeight: FontWeight.bold),
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis),
                        const SizedBox(height: 8),
                        if (details != null && details['artist'] != "")
                          Row(
                            children: [
                              const Icon(Icons.person,
                                  color: Colors.white70, size: 16),
                              const SizedBox(width: 4),
                              Expanded(
                                child: Builder(
                                  builder: (context) {
                                     String a = (details['artist'] == null || details['artist'] == 'null') ? '' : details['artist'];
                                     if (a.isEmpty) a = track['author'] ?? track['channel'] ?? '';
                                     String d = details['release_date'] ?? '';
                                     if (d == 'null') d = '';
                                     if (d.contains('T')) d = d.split('T').first;
                                     String text = [a, d].where((e) => e.isNotEmpty).join(' • ');
                                     return Text(text,
                                        style: const TextStyle(color: Colors.white70, fontSize: 14),
                                        maxLines: 2,
                                        overflow: TextOverflow.ellipsis);
                                  }
                                ),
                              ),
                            ],
                          ),
                      ],
                    ),
                  ),
                ],
              ),
            ),

            // Play & Download Buttons
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
              child: Row(
                children: [
                  IconButton(
                    iconSize: 56,
                    icon: const Icon(Icons.play_circle_fill,
                        color: Colors.white),
                    onPressed: () {
                      audioNotifier.streamYoutube(track['id'] ?? track['videoId'], track);
                      Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
                    },
                  ),
                  const SizedBox(width: 16),
                  IconButton(
                    iconSize: 36,
                    icon: const Icon(Icons.playlist_add, color: Colors.white70),
                    onPressed: () {
                      final playlists = ref.read(playlistProvider);
                      showGlassDialog(
                        context: context,
                        title: 'Add to Playlist',
                        content: SizedBox(
                          width: double.maxFinite,
                          child: playlists.isEmpty 
                            ? const Text('No playlists created yet.', style: TextStyle(color: Colors.white54))
                            : ListView.builder(
                                shrinkWrap: true,
                                itemCount: playlists.length,
                                itemBuilder: (context, i) => ListTile(
                                  title: Text(playlists[i].name, style: const TextStyle(color: Colors.white)),
                                  onTap: () {
                                    ref.read(playlistProvider.notifier).addTrackToPlaylist(playlists[i].id, track);
                                    Navigator.pop(context);
                                    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Added to ${playlists[i].name}'), duration: const Duration(seconds: 1)));
                                  },
                                )
                              )
                        ),
                        actions: [
                          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel', style: TextStyle(color: Colors.white54)))
                        ]
                      );
                    },
                  ),
                  const SizedBox(width: 8),
                  IconButton(
                    iconSize: 36,
                    icon: downloadTask?.phase == DownloadPhase.active
                        ? SizedBox(
                            width: 34,
                            height: 34,
                            child: CircularProgressIndicator(
                              value: downloadTask!.progress > 0
                                  ? downloadTask.progress
                                  : null,
                              strokeWidth: 2.2,
                              color: _accentGrey,
                            ),
                          )
                        : Icon(
                            downloadTask?.phase == DownloadPhase.complete
                                ? Icons.check_circle_outline
                                : Icons.download_for_offline,
                            color: Colors.white70,
                          ),
                    onPressed: videoId == null
                        ? null
                        : () {
                            ref
                                .read(downloadCenterProvider.notifier)
                                .downloadTrack(track);
                          },
                  ),
                ],
              ),
            ),

            // Similar Tracks
            if (details == null)
              const Center(
                  child: Padding(
                      padding: EdgeInsets.all(32),
                      child:
                          CircularProgressIndicator(color: Colors.white)))
            else if (details['similar_tracks'] != null &&
                (details['similar_tracks'] as List).isNotEmpty) ...[
              const Padding(
                padding: EdgeInsets.symmetric(horizontal: 24, vertical: 8),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: Text('Similar Tracks',
                      style: TextStyle(
                          color: Colors.white,
                          fontSize: 20,
                          fontWeight: FontWeight.bold)),
                ),
              ),
              ListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                padding: const EdgeInsets.symmetric(horizontal: 16),
                itemCount: (details['similar_tracks'] as List).length,
                itemBuilder: (context, i) {
                  final st = details['similar_tracks'][i];
                  return ListTile(
                    onTap: () {
                      ref
                          .read(trackDetailsProvider.notifier)
                          .fetchDetails(st['id']);
                      Navigator.pushReplacement(
                          context,
                          MaterialPageRoute(
                              builder: (_) => TrackDetailsScreen(track: st)));
                    },
                    leading: ClipRRect(
                      borderRadius: BorderRadius.circular(4),
                      child: AppArtwork(
                        thumbnail: st['thumbnail'],
                        videoId: st['id']?.toString(),
                        width: 48,
                        height: 48,
                        radius: 10,
                      ),
                    ),
                    title: Text(st['title'] ?? '',
                        style: const TextStyle(color: Colors.white),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis),
                    subtitle: Text(st['channel'] ?? '',
                        style: const TextStyle(color: Colors.white54),
                        maxLines: 1),
                  );
                },
              ),
            ],
            const SizedBox(height: 80),
          ],
        ),
      ),
    );
  }
}

class FadeIndexedStack extends StatefulWidget {
  final int index;
  final List<Widget> children;
  final Duration duration;

  const FadeIndexedStack({
    super.key,
    required this.index,
    required this.children,
    this.duration = const Duration(milliseconds: 300),
  });

  @override
  State<FadeIndexedStack> createState() => _FadeIndexedStackState();
}

class _FadeIndexedStackState extends State<FadeIndexedStack>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;

  @override
  void initState() {
    _controller = AnimationController(vsync: this, duration: widget.duration);
    _controller.forward();
    super.initState();
  }

  @override
  void didUpdateWidget(FadeIndexedStack oldWidget) {
    if (widget.index != oldWidget.index) {
      _controller.forward(from: 0.0);
    }
    super.didUpdateWidget(oldWidget);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FadeTransition(
      opacity: _controller,
      child: IndexedStack(
        index: widget.index,
        children: widget.children,
      ),
    );
  }
}
