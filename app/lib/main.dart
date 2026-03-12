import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'logic/audio_provider.dart';

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
      title: 'Auralis Spotify Clone',
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF121212),
        primaryColor: const Color(0xFF1DB954), // Spotify Green
        appBarTheme: const AppBarTheme(
          backgroundColor: Colors.transparent,
          elevation: 0,
        ),
        bottomNavigationBarTheme: const BottomNavigationBarThemeData(
          backgroundColor: Colors.black,
          selectedItemColor: Colors.white,
          unselectedItemColor: Colors.white54,
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
    final playerState = ref.watch(audioPlayerProvider);

    return Scaffold(
      body: Stack(
        children: [
          FadeIndexedStack(
            index: _currentIndex,
            children: _pages,
          ),
          AnimatedPositioned(
            duration: const Duration(milliseconds: 300),
            curve: Curves.easeOutCubic,
            bottom: playerState.currentTrackName != 'No track loaded'
                ? 0
                : -100, // Slides off screen when not loaded
            left: 0,
            right: 0,
            child: const MiniPlayer(),
          ),
        ],
      ),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _currentIndex,
        onTap: (index) {
          setState(() {
            _currentIndex = index;
          });
        },
        items: const [
          BottomNavigationBarItem(icon: Icon(Icons.home), label: 'Home'),
          BottomNavigationBarItem(
              icon: Icon(Icons.library_music), label: 'Your Library'),
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

  @override
  void initState() {
    super.initState();
    _urlController.addListener(() {
      final text = _urlController.text.trim();
      if (text.isNotEmpty && !_isSearching) {
        ref.read(suggestProvider.notifier).fetchSuggestions(text);
      } else if (text.isEmpty) {
        ref.read(suggestProvider.notifier).clear();
        setState(() => _isSearching = false);
      }
    });
  }

  @override
  void dispose() {
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

  Future<void> _triggerDownload(String videoId, String trackName) async {
    final dir = await getApplicationDocumentsDirectory();
    final cleanName = trackName.replaceAll(RegExp(r'[<>:"/\\|?*]'), '_');
    final outPath = '${dir.path}/$cleanName.mp3';
    ref
        .read(audioPlayerProvider.notifier)
        .downloadAndLoadYoutube(videoId, outPath);
  }

  Widget _buildTrackList(
      List<dynamic> tracks, bool isLoading, String emptyMessage) {
    if (isLoading && tracks.isEmpty) {
      return const Center(
          child: CircularProgressIndicator(color: Color(0xFF1DB954)));
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
                  child: CircularProgressIndicator(color: Color(0xFF1DB954))));
        }
        final t = tracks[index];
        return ListTile(
          onTap: () {
            ref.read(trackDetailsProvider.notifier).fetchDetails(t['id']);
            Navigator.push(
                context,
                MaterialPageRoute(
                    builder: (_) => TrackDetailsScreen(track: t)));
          },
          contentPadding: const EdgeInsets.symmetric(vertical: 8),
          leading: ClipRRect(
            borderRadius: BorderRadius.circular(4),
            child: t['thumbnail'] != null
                ? Image.network(t['thumbnail'],
                    width: 60, height: 60, fit: BoxFit.cover)
                : Container(width: 60, height: 60, color: Colors.grey[800]),
          ),
          title: Text(t['title'] ?? 'Unknown',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                  color: Colors.white, fontWeight: FontWeight.w600)),
          subtitle: Text(t['channel'] ?? '',
              maxLines: 1, style: const TextStyle(color: Colors.white54)),
          trailing: IconButton(
            icon: const Icon(Icons.download, color: Color(0xFF1DB954)),
            onPressed: () => _triggerDownload(t['id'], t['title']),
          ),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    final playerState = ref.watch(audioPlayerProvider);
    final searchState = ref.watch(searchProvider);
    final isSearchLoading = ref.watch(searchProvider.notifier).isLoading;

    final recState = ref.watch(recommendationProvider);
    final isRecLoading = ref.watch(recommendationProvider.notifier).isLoading;
    final suggestState = ref.watch(suggestProvider);

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
            const Text(
              'Good evening',
              style: TextStyle(
                  fontSize: 24,
                  fontWeight: FontWeight.bold,
                  color: Colors.white),
            ),
            const SizedBox(height: 32),

            TextField(
              controller: _urlController,
              focusNode: _searchFocusNode,
              style: const TextStyle(color: Colors.white),
              decoration: InputDecoration(
                hintText: 'Search songs or paste URL...',
                hintStyle: const TextStyle(color: Colors.white54),
                filled: true,
                fillColor: Colors.grey[900],
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
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
            if (playerState.isDownloading) ...[
              const LinearProgressIndicator(color: Color(0xFF1DB954)),
              const SizedBox(height: 16),
            ],

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
              const Text('Made For You',
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

    return SafeArea(
      child: libraryAsync.when(
        data: (files) {
          if (files.isEmpty) {
            return const Center(
              child: Text(
                'Your Offline Library is empty.',
                style: TextStyle(color: Colors.white54, fontSize: 18),
              ),
            );
          }
          return ListView.builder(
            padding: const EdgeInsets.all(16),
            itemCount: files.length,
            itemBuilder: (context, index) {
              final track = files[index];
              final name = track['title'] ?? 'Unknown';
              final path = track['local_path'];
              return ListTile(
                leading: ClipRRect(
                  borderRadius: BorderRadius.circular(4),
                  child: track['thumbnail'] != null
                      ? Image.network(track['thumbnail'],
                          width: 50, height: 50, fit: BoxFit.cover)
                      : Container(
                          width: 50,
                          height: 50,
                          color: Colors.grey[800],
                          child: const Icon(Icons.music_note,
                              color: Colors.white)),
                ),
                title: Text(name,
                    style: const TextStyle(color: Colors.white),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis),
                subtitle: Text(track['author'] ?? track['artist'] ?? '',
                    style: const TextStyle(color: Colors.white54, fontSize: 12),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis),
                trailing: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      track['filesize'] != null && track['filesize'] > 0
                          ? '${(track['filesize'] / 1024 / 1024).toStringAsFixed(1)} MB'
                          : '',
                      style:
                          const TextStyle(color: Colors.white38, fontSize: 10),
                    ),
                    IconButton(
                      icon: const Icon(Icons.delete,
                          color: Colors.redAccent, size: 20),
                      onPressed: () {
                        try {
                          File(path).deleteSync();
                          final jsonPath = path.replaceAll('.mp3', '.json');
                          if (File(jsonPath).existsSync()) {
                            File(jsonPath).deleteSync();
                          }
                          ref.invalidate(libraryProvider);
                        } catch (e) {
                          debugPrint('Delete failed: $e');
                        }
                      },
                    ),
                  ],
                ),
                onTap: () {
                  if (track['thumbnail'] != null) {
                    ref
                        .read(audioPlayerProvider.notifier)
                        .loadLocalWithMeta(path, track);
                  } else {
                    ref
                        .read(audioPlayerProvider.notifier)
                        .loadLocalFile(path, name);
                  }
                  ref.read(audioPlayerProvider.notifier).play();
                },
              );
            },
          );
        },
        loading: () => const Center(
            child: CircularProgressIndicator(color: Color(0xFF1DB954))),
        error: (err, stack) => Center(
            child:
                Text('Error: $err', style: const TextStyle(color: Colors.red))),
      ),
    );
  }
}

class MiniPlayer extends ConsumerWidget {
  const MiniPlayer({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final playerState = ref.watch(audioPlayerProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);

    return GestureDetector(
      onTap: () {
        Navigator.of(context)
            .push(MaterialPageRoute(builder: (_) => const FullPlayerScreen()));
      },
      child: Container(
        height: 70,
        margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 8),
        decoration: BoxDecoration(
          color: Colors.grey[900],
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          children: [
            Expanded(
              child: Row(
                children: [
                  const SizedBox(width: 8),
                  ClipRRect(
                    borderRadius: BorderRadius.circular(4),
                    child: playerState.thumbnail != null
                        ? Image.network(playerState.thumbnail!,
                            width: 44, height: 44, fit: BoxFit.cover)
                        : Container(
                            width: 44,
                            height: 44,
                            color: Colors.grey[800],
                            child: const Icon(Icons.music_note,
                                color: Colors.white)),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Text(
                          playerState.currentTrackName,
                          style: const TextStyle(
                              color: Colors.white, fontWeight: FontWeight.w600),
                          overflow: TextOverflow.ellipsis,
                        ),
                        if (playerState.duration > 0)
                          Text(
                              '${(playerState.currentPosition / 60).floor()}:${(playerState.currentPosition % 60).toString().padLeft(2, '0')} / ${(playerState.duration / 60).floor()}:${(playerState.duration % 60).toString().padLeft(2, '0')}',
                              style: const TextStyle(
                                  color: Colors.white54, fontSize: 10)),
                      ],
                    ),
                  ),
                  IconButton(
                    icon: Icon(
                        playerState.isPlaying ? Icons.pause : Icons.play_arrow,
                        color: Colors.white),
                    onPressed: () {
                      playerState.isPlaying
                          ? audioNotifier.pause()
                          : audioNotifier.play();
                    },
                  ),
                ],
              ),
            ),
            if (playerState.duration > 0)
              LinearProgressIndicator(
                value: (playerState.currentPosition / playerState.duration)
                    .clamp(0.0, 1.0),
                backgroundColor: Colors.transparent,
                color: Colors.white,
                minHeight: 2,
              )
          ],
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

    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.keyboard_arrow_down),
          onPressed: () => Navigator.pop(context),
        ),
        title: const Text('Playing from Library',
            style: TextStyle(fontSize: 12, color: Colors.white54)),
        centerTitle: true,
      ),
      body: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 24.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const SizedBox(height: 40),
            Center(
              child: Container(
                width: 300,
                height: 300,
                color: Colors.grey[850],
                child: playerState.thumbnail != null
                    ? Image.network(playerState.thumbnail!, fit: BoxFit.cover)
                    : const Icon(Icons.audiotrack,
                        size: 120, color: Colors.grey),
              ),
            ),
            const SizedBox(height: 48),
            Text(
              playerState.currentTrackName,
              style: const TextStyle(
                  fontSize: 24,
                  fontWeight: FontWeight.bold,
                  color: Colors.white),
              overflow: TextOverflow.ellipsis,
            ),
            Text(
              playerState.artist ?? 'Artist Unknown',
              style: const TextStyle(fontSize: 16, color: Colors.white54),
            ),
            const SizedBox(height: 32),
            // Progress Bar Placeholder
            SliderTheme(
              data: SliderTheme.of(context).copyWith(
                activeTrackColor: Colors.white,
                inactiveTrackColor: Colors.grey[800],
                thumbColor: Colors.white,
                trackHeight: 4.0,
              ),
              child: Slider(
                value: playerState.duration > 0
                    ? _dragValue ??
                        (playerState.currentPosition / playerState.duration)
                            .clamp(0.0, 1.0)
                    : 0.0,
                onChanged: playerState.duration > 0
                    ? (val) {
                        setState(() {
                          _dragValue = val;
                        });
                      }
                    : null,
                onChangeEnd: playerState.duration > 0
                    ? (val) {
                        audioNotifier
                            .seek((val * playerState.duration).toInt());
                        setState(() {
                          _dragValue = null;
                        });
                      }
                    : null,
              ),
            ),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                    '${(playerState.currentPosition / 60).floor()}:${(playerState.currentPosition % 60).toString().padLeft(2, '0')}',
                    style:
                        const TextStyle(color: Colors.white54, fontSize: 12)),
                Text(
                    '${(playerState.duration / 60).floor()}:${(playerState.duration % 60).toString().padLeft(2, '0')}',
                    style:
                        const TextStyle(color: Colors.white54, fontSize: 12)),
              ],
            ),
            const SizedBox(height: 16),
            // Controls
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                IconButton(
                  icon: Icon(Icons.loop,
                      color: playerState.isLooping
                          ? const Color(0xFF1DB954)
                          : Colors.white),
                  onPressed: () {
                    audioNotifier.toggleLoop(0,
                        playerState.duration * 1000); // Standard track looping
                  },
                ),
                const Icon(Icons.skip_previous, color: Colors.white, size: 36),
                GestureDetector(
                  onTap: () {
                    playerState.isPlaying
                        ? audioNotifier.pause()
                        : audioNotifier.play();
                  },
                  child: Container(
                    decoration: const BoxDecoration(
                        shape: BoxShape.circle, color: Colors.white),
                    padding: const EdgeInsets.all(16),
                    child: Icon(
                        playerState.isPlaying ? Icons.pause : Icons.play_arrow,
                        color: Colors.black,
                        size: 32),
                  ),
                ),
                const Icon(Icons.skip_next, color: Colors.white, size: 36),
                const Icon(Icons.shuffle, color: Colors.white54),
              ],
            )
          ],
        ),
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
                  ClipRRect(
                    borderRadius: BorderRadius.circular(8),
                    child: track['thumbnail'] != null
                        ? Image.network(track['thumbnail'],
                            width: 140, height: 140, fit: BoxFit.cover)
                        : Container(
                            width: 140, height: 140, color: Colors.grey[800]),
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
                                child: Text(
                                    '${details['artist']} • ${details['release_date']}'
                                        .replaceAll(RegExp(r' • $|^ • '), ''),
                                    style: const TextStyle(
                                        color: Colors.white70, fontSize: 14),
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis),
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
                        color: Color(0xFF1DB954)),
                    onPressed: () async {
                      final dir = await getApplicationDocumentsDirectory();
                      final cleanName = (track['title'] ?? 'Unknown Track')
                          .replaceAll(RegExp(r'[<>:"/\\|?*]'), '_');
                      audioNotifier.downloadAndLoadYoutube(
                          track['id'], '${dir.path}/$cleanName.mp3');
                    },
                  ),
                  const SizedBox(width: 16),
                  IconButton(
                    iconSize: 36,
                    icon: const Icon(Icons.download_for_offline,
                        color: Colors.white70),
                    onPressed: () async {
                      final dir = await getApplicationDocumentsDirectory();
                      final cleanName = (track['title'] ?? 'Unknown Track')
                          .replaceAll(RegExp(r'[<>:"/\\|?*]'), '_');
                      audioNotifier.downloadAndLoadYoutube(
                          track['id'], '${dir.path}/$cleanName.mp3');
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
                          CircularProgressIndicator(color: Color(0xFF1DB954))))
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
                      child: st['thumbnail'] != null
                          ? Image.network(st['thumbnail'],
                              width: 48, height: 48, fit: BoxFit.cover)
                          : Container(
                              width: 48, height: 48, color: Colors.grey[800]),
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
