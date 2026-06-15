import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../logic/assistant_provider.dart';
import '../logic/audio_provider.dart';
import '../logic/audio_provider_queue.dart';
import '../logic/auth_provider.dart';
import '../logic/download_provider.dart';
import '../logic/playlist_provider.dart';
import '../ui/app_theme_tokens.dart';
import '../ui/neatie_components.dart';
import '../widgets/app_artwork.dart';

const _assistantBg = neatieInk;
const _assistantSurface = neatieRaised;
const _assistantSurfaceAlt = neatieGlass;
const _assistantAccent = neatieActive;

typedef AssistantAlbumOpener = Future<void> Function(Map<String, dynamic> album);

class AssistantScreen extends ConsumerStatefulWidget {
  final AssistantAlbumOpener? onOpenAlbum;
  final VoidCallback? onOpenPlayer;

  const AssistantScreen({
    super.key,
    this.onOpenAlbum,
    this.onOpenPlayer,
  });

  @override
  ConsumerState<AssistantScreen> createState() => _AssistantScreenState();
}

class _AssistantScreenState extends ConsumerState<AssistantScreen> {
  final TextEditingController _composerController = TextEditingController();
  final ScrollController _scrollController = ScrollController();
  String? _bootstrappedScopeId;

  @override
  void initState() {
    super.initState();
    Future.microtask(
      () => ref.read(assistantProvider.notifier).ensureInitialized(),
    );
  }

  @override
  void dispose() {
    _composerController.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final message = _composerController.text.trim();
    if (message.isEmpty) return;
    _composerController.clear();
    await ref.read(assistantProvider.notifier).sendMessage(message);
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 120));
    if (_scrollController.hasClients) {
      _scrollController.animateTo(
        _scrollController.position.maxScrollExtent,
        duration: const Duration(milliseconds: 320),
        curve: Curves.easeOutCubic,
      );
    }
  }

  Future<void> _playTrack(Map<String, dynamic> track) async {
    await ref.read(playbackQueueProvider.notifier).startRadioSession(track);
  }

  Future<void> _queueNext(Map<String, dynamic> track) async {
    await ref.read(playbackQueueProvider.notifier).enqueueTrack(track, playNext: true);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('${track['title'] ?? 'Track'} added next'),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  Future<void> _downloadTrack(Map<String, dynamic> track) async {
    await ref.read(downloadCenterProvider.notifier).downloadTrack(track);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Downloading ${track['title'] ?? 'track'}'),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  Future<void> _playSuggestedSet(List<AssistantTrackCard> tracks) async {
    if (tracks.isEmpty) return;
    await ref.read(playbackQueueProvider.notifier).startRadioSession(tracks.first.track);
    for (final track in tracks.skip(1)) {
      await ref.read(playbackQueueProvider.notifier).enqueueTrack(
            track.track,
            playNext: false,
          );
    }
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Playing ${tracks.length} suggested songs'),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  Future<void> _queueSuggestedSet(List<AssistantTrackCard> tracks) async {
    if (tracks.isEmpty) return;
    for (final track in tracks.reversed) {
      await ref.read(playbackQueueProvider.notifier).enqueueTrack(
            track.track,
            playNext: true,
          );
    }
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Queued ${tracks.length} suggested songs'),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  Future<void> _saveSuggestedSet(List<AssistantTrackCard> tracks) async {
    if (tracks.isEmpty) return;
    for (final track in tracks) {
      await upsertCloudLibraryTrack(track.track);
    }
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Saved ${tracks.length} songs to your account'),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  Future<String?> _promptText({
    required String title,
    required String initialValue,
    required String confirmLabel,
  }) async {
    return Navigator.of(context, rootNavigator: true).push<String>(
      MaterialPageRoute(
        fullscreenDialog: true,
        builder: (context) => _AssistantTextPromptPage(
          title: title,
          initialValue: initialValue,
          confirmLabel: confirmLabel,
        ),
      ),
    );
  }

  Future<void> _saveDraftPlaylist(AssistantPlaylistDraft draft) async {
    final playlistName = await _promptText(
          title: 'Playlist name',
          initialValue: draft.name,
          confirmLabel: 'Save',
        ) ??
        draft.name;
    final playlistNotifier = ref.read(playlistProvider.notifier);
    final playlist = playlistNotifier.createPlaylist(playlistName);
    for (final track in draft.tracks) {
      playlistNotifier.addTrackToPlaylist(playlist.id, track.track);
    }
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Created playlist "$playlistName"'),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  Future<void> _addTracksToPlaylist(
    String playlistId,
    String playlistName,
    List<AssistantTrackCard> tracks,
  ) async {
    final playlistNotifier = ref.read(playlistProvider.notifier);
    for (final track in tracks) {
      playlistNotifier.addTrackToPlaylist(playlistId, track.track);
    }
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Added ${tracks.length} songs to "$playlistName"'),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  Future<void> _choosePlaylistForTracks(List<AssistantTrackCard> tracks) async {
    final playlists = ref.read(playlistProvider);
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: _assistantSurfaceAlt,
      builder: (context) {
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Material(
                type: MaterialType.transparency,
                child: ListTile(
                  leading: const Icon(Icons.add_rounded, color: Colors.white),
                  title: const Text(
                    'New playlist',
                    style: TextStyle(color: Colors.white),
                  ),
                  onTap: () async {
                    Navigator.of(context).pop();
                    final playlistName = await _promptText(
                      title: 'Playlist name',
                      initialValue: '',
                      confirmLabel: 'Create',
                    );
                    if (playlistName == null) return;
                    final playlist = ref
                        .read(playlistProvider.notifier)
                        .createPlaylist(playlistName);
                    await _addTracksToPlaylist(
                      playlist.id,
                      playlist.name,
                      tracks,
                    );
                  },
                ),
              ),
              ...playlists.map((playlist) {
                return Material(
                  type: MaterialType.transparency,
                  child: ListTile(
                    leading: const Icon(
                      Icons.queue_music_rounded,
                      color: Colors.white70,
                    ),
                    title: Text(
                      playlist.name,
                      style: const TextStyle(color: Colors.white),
                    ),
                    subtitle: Text(
                      '${playlist.tracks.length} tracks',
                      style: TextStyle(
                        color: Colors.white.withValues(alpha: 0.5),
                      ),
                    ),
                    onTap: () async {
                      Navigator.of(context).pop();
                      await _addTracksToPlaylist(
                        playlist.id,
                        playlist.name,
                        tracks,
                      );
                    },
                  ),
                );
              }),
            ],
          ),
        );
      },
    );
  }

  Future<void> _sendSeed(String value) async {
    _composerController.text = value;
    await _send();
  }

  void _prefillComposer(String value) {
    _composerController
      ..text = value
      ..selection = TextSelection.collapsed(offset: value.length);
    if (mounted) {
      setState(() {});
    }
  }

  Future<void> _retryLastTurn() async {
    await ref.read(assistantProvider.notifier).retryLastTurn();
    if (!mounted) return;
    await Future<void>.delayed(const Duration(milliseconds: 120));
    if (_scrollController.hasClients) {
      _scrollController.animateTo(
        _scrollController.position.maxScrollExtent,
        duration: const Duration(milliseconds: 320),
        curve: Curves.easeOutCubic,
      );
    }
  }

  List<Widget> _suggestionRefinementChips(AssistantMessage message) {
    if (message.tracks.isEmpty) return const [];
    final firstTrack = message.tracks.first.track;
    final firstTitle = (firstTrack['title'] ?? 'that song').toString().trim();
    final firstArtist =
        (firstTrack['channel'] ?? firstTrack['artist'] ?? 'that artist').toString().trim();
    return [
      _ActionChip(
        icon: Icons.auto_awesome_rounded,
        label: 'More like this',
        onTap: () => _sendSeed(
          message.tracks.length == 1
              ? 'Give me more songs like "$firstTitle" by $firstArtist.'
              : 'Give me more songs in this direction.',
        ),
      ),
      _ActionChip(
        icon: Icons.shuffle_rounded,
        label: 'Different direction',
        onTap: () => _sendSeed(
          message.tracks.length == 1
              ? 'Give me something different from "$firstTitle" by $firstArtist, but still fitting the mood.'
              : 'Give me a different direction from those suggestions, but keep the same mood.',
        ),
      ),
      _ActionChip(
        icon: Icons.dark_mode_rounded,
        label: 'Make it darker',
        onTap: () => _sendSeed('Make it darker.'),
      ),
      _ActionChip(
        icon: Icons.history_edu_rounded,
        label: 'Make it older',
        onTap: () => _sendSeed('Make it older.'),
      ),
      _ActionChip(
        icon: Icons.bolt_rounded,
        label: 'More energetic',
        onTap: () => _sendSeed('Make it more energetic.'),
      ),
      if (message.playlistDraft == null)
        _ActionChip(
          icon: Icons.playlist_add_check_rounded,
          label: 'Turn into playlist',
          onTap: () => _sendSeed('Turn those songs into a playlist.'),
        ),
    ];
  }

  Future<void> _openExternal(String url) async {
    final normalized = url.trim();
    if (normalized.isEmpty) return;
    final uri = Uri.tryParse(normalized);
    if (uri == null) return;
    final opened = await launchUrl(uri, mode: LaunchMode.externalApplication);
    if (!opened && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Could not open that source link'),
          duration: Duration(seconds: 2),
        ),
      );
    }
  }

  Future<void> _openSessionsSheet() async {
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: _assistantSurfaceAlt,
      isScrollControlled: true,
      builder: (context) {
        return SafeArea(
              child: Consumer(
            builder: (context, ref, _) {
              final state = ref.watch(assistantProvider);
              final pinnedSessions =
                  state.sessions.where((session) => session.isPinned && !session.isArchived).toList();
              final activeSessions =
                  state.sessions.where((session) => !session.isPinned && !session.isArchived).toList();
              final archivedSessions =
                  state.sessions.where((session) => session.isArchived).toList();
              return Padding(
                padding: const EdgeInsets.fromLTRB(12, 12, 12, 20),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Row(
                      children: [
                        const Expanded(
                          child: Text(
                            'Chats',
                            style: TextStyle(
                              color: Colors.white,
                              fontSize: 18,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                        TextButton.icon(
                          onPressed: () {
                            ref.read(assistantProvider.notifier).clearConversation();
                            Navigator.of(context).pop();
                          },
                          icon: const Icon(Icons.add_comment_rounded, size: 18),
                          label: const Text('New'),
                        ),
                      ],
                    ),
                    const SizedBox(height: 8),
                    Flexible(
                      child: state.sessions.isEmpty
                          ? Center(
                              child: Padding(
                                padding: const EdgeInsets.all(24),
                                child: Text(
                                  'Your assistant chats will appear here once you start one.',
                                  textAlign: TextAlign.center,
                                  style: TextStyle(
                                    color: Colors.white.withValues(alpha: 0.62),
                                  ),
                                ),
                              ),
                            )
                          : ListView(
                              shrinkWrap: true,
                              children: [
                                if (pinnedSessions.isNotEmpty) ...[
                                  const _SessionSectionHeader(title: 'Pinned'),
                                  ...pinnedSessions.map(
                                    (session) => _SessionTile(
                                      session: session,
                                      selected: session.id == state.currentSessionId,
                                      onTap: () async {
                                        Navigator.of(context).pop();
                                        await ref
                                            .read(assistantProvider.notifier)
                                            .openSession(session.id);
                                      },
                                      onRename: () async {
                                        final title = await _promptText(
                                          title: 'Rename chat',
                                          initialValue: session.title,
                                          confirmLabel: 'Save',
                                        );
                                        if (title != null) {
                                          await ref
                                              .read(assistantProvider.notifier)
                                              .renameSession(session.id, title);
                                        }
                                      },
                                      onTogglePin: () => ref
                                          .read(assistantProvider.notifier)
                                          .pinSession(session.id, !session.isPinned),
                                      onToggleArchive: () => ref
                                          .read(assistantProvider.notifier)
                                          .archiveSession(session.id, !session.isArchived),
                                      onDelete: () => ref
                                          .read(assistantProvider.notifier)
                                          .deleteSession(session.id),
                                    ),
                                  ),
                                ],
                                if (activeSessions.isNotEmpty) ...[
                                  _SessionSectionHeader(
                                    title: pinnedSessions.isEmpty ? 'Recent chats' : 'Recent',
                                  ),
                                  ...activeSessions.map(
                                    (session) => _SessionTile(
                                      session: session,
                                      selected: session.id == state.currentSessionId,
                                      onTap: () async {
                                        Navigator.of(context).pop();
                                        await ref
                                            .read(assistantProvider.notifier)
                                            .openSession(session.id);
                                      },
                                      onRename: () async {
                                        final title = await _promptText(
                                          title: 'Rename chat',
                                          initialValue: session.title,
                                          confirmLabel: 'Save',
                                        );
                                        if (title != null) {
                                          await ref
                                              .read(assistantProvider.notifier)
                                              .renameSession(session.id, title);
                                        }
                                      },
                                      onTogglePin: () => ref
                                          .read(assistantProvider.notifier)
                                          .pinSession(session.id, !session.isPinned),
                                      onToggleArchive: () => ref
                                          .read(assistantProvider.notifier)
                                          .archiveSession(session.id, !session.isArchived),
                                      onDelete: () => ref
                                          .read(assistantProvider.notifier)
                                          .deleteSession(session.id),
                                    ),
                                  ),
                                ],
                                if (archivedSessions.isNotEmpty) ...[
                                  const _SessionSectionHeader(title: 'Archived'),
                                  ...archivedSessions.map(
                                    (session) => _SessionTile(
                                      session: session,
                                      selected: session.id == state.currentSessionId,
                                      onTap: () async {
                                        Navigator.of(context).pop();
                                        await ref
                                            .read(assistantProvider.notifier)
                                            .openSession(session.id);
                                      },
                                      onRename: () async {
                                        final title = await _promptText(
                                          title: 'Rename chat',
                                          initialValue: session.title,
                                          confirmLabel: 'Save',
                                        );
                                        if (title != null) {
                                          await ref
                                              .read(assistantProvider.notifier)
                                              .renameSession(session.id, title);
                                        }
                                      },
                                      onTogglePin: () => ref
                                          .read(assistantProvider.notifier)
                                          .pinSession(session.id, !session.isPinned),
                                      onToggleArchive: () => ref
                                          .read(assistantProvider.notifier)
                                          .archiveSession(session.id, !session.isArchived),
                                      onDelete: () => ref
                                          .read(assistantProvider.notifier)
                                          .deleteSession(session.id),
                                    ),
                                  ),
                                ],
                              ],
                            ),
                    ),
                  ],
                ),
              );
            },
          ),
        );
      },
    );
  }

  Widget _buildTrackCard(AssistantTrackCard card) {
    final track = card.track;
    final videoId = extractTrackId(track);
    final albumId = track['album_id']?.toString().trim();
    final albumTitle = (track['album'] ?? '').toString().trim();
    final canOpenAlbum = widget.onOpenAlbum != null &&
        albumId != null &&
        albumId.isNotEmpty &&
        albumTitle.isNotEmpty;
    return NeatieSurface(
      margin: const EdgeInsets.only(bottom: 12),
      radius: neatieRadiusMedium,
      color: Colors.white.withValues(alpha: 0.03),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                AppArtwork(
                  thumbnail: track['thumbnail'],
                  videoId: videoId,
                  width: 68,
                  height: 68,
                  radius: 14,
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        track['title']?.toString() ?? 'Unknown Track',
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
                        (track['channel'] ?? track['artist'] ?? '').toString(),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.65),
                          fontSize: 13,
                        ),
                      ),
                      const SizedBox(height: 6),
                      Wrap(
                        spacing: 6,
                        runSpacing: 6,
                        children: [
                          if ((track['album'] ?? '').toString().isNotEmpty)
                            _MetadataChip(
                              label: track['album'].toString(),
                              onTap: canOpenAlbum
                                  ? () => widget.onOpenAlbum!(
                                        {
                                          'id': albumId,
                                          'title': albumTitle,
                                          'artist':
                                              (track['channel'] ?? track['artist'] ?? '')
                                                  .toString(),
                                          'thumbnail': track['thumbnail'],
                                          'year': '',
                                        },
                                      )
                                  : null,
                            ),
                          if (_parseAssistantDuration(track['duration']) > 0)
                            _MetadataChip(
                              label: _formatDuration(
                                _parseAssistantDuration(track['duration']),
                              ),
                            ),
                        ],
                      ),
                    ],
                  ),
                ),
              ],
            ),
            if (card.reason != null && card.reason!.trim().isNotEmpty) ...[
              const SizedBox(height: 10),
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  card.reason!,
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.72),
                    fontSize: 12,
                    height: 1.35,
                  ),
                ),
              ),
            ],
            const SizedBox(height: 10),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                _IconActionButton(
                  icon: Icons.play_arrow_rounded,
                  tooltip: 'Play',
                  onTap: () => _playTrack(track),
                ),
                _IconActionButton(
                  icon: Icons.queue_music_rounded,
                  tooltip: 'Queue next',
                  onTap: () => _queueNext(track),
                ),
                _IconActionButton(
                  icon: Icons.playlist_add_rounded,
                  tooltip: 'Add to playlist',
                  onTap: () => _choosePlaylistForTracks([card]),
                ),
                _IconActionButton(
                  icon: Icons.download_rounded,
                  tooltip: 'Download',
                  onTap: () => _downloadTrack(track),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildMessageBubble(
    AssistantMessage message, {
    bool showUserActions = false,
  }) {
    final isUser = message.role == 'user';
    final showTrackBatchActions = message.tracks.length > 1;
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        constraints: const BoxConstraints(maxWidth: 640),
        margin: const EdgeInsets.only(bottom: 14),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(
          color: isUser
              ? Colors.white.withValues(alpha: 0.08)
              : Colors.white.withValues(alpha: 0.04),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (message.text.trim().isNotEmpty)
              Text(
                _sanitizeAssistantText(message.text),
                style: const TextStyle(color: Colors.white, height: 1.4),
              ),
            if (message.followUpQuestion != null &&
                message.followUpQuestion!.isNotEmpty) ...[
              if (message.text.trim().isNotEmpty) const SizedBox(height: 8),
              Text(
                _sanitizeAssistantText(message.followUpQuestion!),
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.72),
                  fontSize: 13,
                ),
              ),
            ],
            if (message.tracks.isNotEmpty) ...[
              const SizedBox(height: 12),
              ...message.tracks.map(_buildTrackCard),
              if (showTrackBatchActions) ...[
                const SizedBox(height: 2),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    _ActionChip(
                      icon: Icons.play_arrow_rounded,
                      label: 'Play all',
                      onTap: () => _playSuggestedSet(message.tracks),
                    ),
                    _ActionChip(
                      icon: Icons.queue_music_rounded,
                      label: 'Queue all',
                      onTap: () => _queueSuggestedSet(message.tracks),
                    ),
                    _ActionChip(
                      icon: Icons.playlist_add_rounded,
                      label: 'Add all to playlist',
                      onTap: () => _choosePlaylistForTracks(message.tracks),
                    ),
                    _ActionChip(
                      icon: Icons.library_add_check_rounded,
                      label: 'Save all',
                      onTap: () => _saveSuggestedSet(message.tracks),
                    ),
                  ],
                ),
              ],
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: _suggestionRefinementChips(message),
              ),
            ],
            if (message.factCards.isNotEmpty) ...[
              const SizedBox(height: 12),
              ...message.factCards.map(
                (fact) => _AssistantFactCard(
                  fact: fact,
                  onOpenSource:
                      fact.sourceUrl == null || fact.sourceUrl!.trim().isEmpty
                          ? null
                          : () => _openExternal(fact.sourceUrl!),
                ),
              ),
            ],
            if (message.sourceLinks.isNotEmpty) ...[
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: message.sourceLinks.map((link) {
                  return _ActionChip(
                    icon: Icons.open_in_new_rounded,
                    label: link.label,
                    onTap: () => _openExternal(link.url),
                  );
                }).toList(growable: false),
              ),
            ],
            if (message.clarificationOptions.isNotEmpty) ...[
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: message.clarificationOptions.map((option) {
                  final description = option.description.trim();
                  final label =
                      description.isEmpty ? option.label : '${option.label} - $description';
                  return ActionChip(
                    backgroundColor: Colors.white.withValues(alpha: 0.08),
                    label: Text(
                      label,
                      style: const TextStyle(color: Colors.white),
                    ),
                    onPressed: () => _sendSeed(option.value),
                  );
                }).toList(growable: false),
              ),
            ],
            if (message.playlistDraft != null) ...[
              const SizedBox(height: 10),
              _PlaylistDraftCard(
                draft: message.playlistDraft!,
                onPlayNow: () => _playSuggestedSet(message.playlistDraft!.tracks),
                onCreate: () => _saveDraftPlaylist(message.playlistDraft!),
                onAddToExisting: () =>
                    _choosePlaylistForTracks(message.playlistDraft!.tracks),
              ),
            ],
            if (message.targetPlaylist != null && message.tracks.isNotEmpty) ...[
              const SizedBox(height: 10),
              _ActionChip(
                icon: Icons.playlist_add_check_rounded,
                label: 'Add to ${message.targetPlaylist!.name}',
                onTap: () => _addTracksToPlaylist(
                  message.targetPlaylist!.id,
                  message.targetPlaylist!.name,
                  message.tracks,
                ),
              ),
            ],
            if (message.playlistOptions.isNotEmpty) ...[
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: message.playlistOptions.map((playlist) {
                  return ActionChip(
                    backgroundColor: Colors.white.withValues(alpha: 0.08),
                    label: Text(
                      playlist.name,
                      style: const TextStyle(color: Colors.white),
                    ),
                    onPressed: () {
                      _composerController.text =
                          'Add those songs to my "${playlist.name}" playlist';
                      _send();
                    },
                  );
                }).toList(growable: false),
              ),
            ],
            if (isUser && showUserActions) ...[
              const SizedBox(height: 8),
              Align(
                alignment: Alignment.centerRight,
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    _TinyIconButton(
                      icon: Icons.refresh_rounded,
                      tooltip: 'Retry response',
                      onTap: _retryLastTurn,
                    ),
                    const SizedBox(width: 4),
                    _TinyIconButton(
                      icon: Icons.edit_outlined,
                      tooltip: 'Edit prompt',
                      onTap: () => _prefillComposer(message.text),
                    ),
                  ],
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildStatusCard(String label) {
    return NeatieSurface(
      margin: const EdgeInsets.only(bottom: 14),
      padding: const EdgeInsets.all(14),
      radius: neatieRadiusMedium,
      color: Colors.white.withValues(alpha: 0.03),
      child: Row(
        children: [
          const SizedBox(
            width: 18,
            height: 18,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: _assistantAccent,
            ),
          ),
          const SizedBox(width: 12),
          Text(
            label,
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.72),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildChatList(AssistantState assistantState) {
    final lastUserIndex =
        assistantState.messages.lastIndexWhere((message) => message.role == 'user');
    return Column(
      key: const ValueKey('assistant-chat'),
      children: [
        Expanded(
          child: ListView(
            controller: _scrollController,
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
            children: [
              ...assistantState.messages.asMap().entries.map(
                (entry) => _buildMessageBubble(
                  entry.value,
                  showUserActions:
                      entry.key == lastUserIndex && !assistantState.isSending,
                ),
              ),
              if (assistantState.isLoadingSession)
                _buildStatusCard('Opening chat…'),
              if (assistantState.isSending) _buildStatusCard('Thinking through it...'),
            ],
          ),
        ),
        _ComposerPanel(
          controller: _composerController,
          isSending: assistantState.isSending,
          showInlinePlayer:
              ref.watch(audioPlayerProvider).currentTrackName != 'No track loaded',
          onSend: _send,
          onOpenPlayer: widget.onOpenPlayer,
        ),
      ],
    );
  }

  Widget _buildFreshSession(AssistantState assistantState) {
    const suggestions = [
      'Comfort me',
      'Find songs like...',
      'Surprise me',
    ];
    return Center(
      key: const ValueKey('assistant-empty'),
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(24, 24, 24, 28),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 760),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const Text(
                'What are we listening for today?',
                textAlign: TextAlign.center,
                style: TextStyle(
                  color: Colors.white,
                  fontSize: 34,
                  fontWeight: FontWeight.w500,
                  height: 1.1,
                ),
              ),
              const SizedBox(height: 28),
              _FreshComposerPanel(
                controller: _composerController,
                isSending: assistantState.isSending,
                onSend: _send,
              ),
              const SizedBox(height: 18),
              Wrap(
                alignment: WrapAlignment.center,
                spacing: 10,
                runSpacing: 10,
                children: suggestions.map((suggestion) {
                  return _ActionChip(
                    icon: Icons.auto_awesome_rounded,
                    label: suggestion,
                    onTap: () {
                      if (suggestion == 'Find songs like...') {
                        _prefillComposer('Find songs like ');
                        return;
                      }
                      if (suggestion == 'Surprise me') {
                        _sendSeed(
                          'Surprise me with music I might love. Use my taste and give me playable tracks.',
                        );
                        return;
                      }
                      _sendSeed(suggestion);
                    },
                  );
                }).toList(growable: false),
              ),
              if (assistantState.error != null && assistantState.error!.trim().isNotEmpty) ...[
                const SizedBox(height: 20),
                Text(
                  assistantState.error!,
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: Colors.white.withValues(alpha: 0.58),
                    fontSize: 12,
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final scopeId = ref.watch(authProvider.select((state) => state.storageScopeId));
    if (_bootstrappedScopeId != scopeId) {
      _bootstrappedScopeId = scopeId;
      Future.microtask(
        () => ref.read(assistantProvider.notifier).ensureInitialized(force: true),
      );
    }

    final assistantState = ref.watch(assistantProvider);
    final isFreshSession = assistantState.messages.isEmpty &&
        !assistantState.isLoadingSession &&
        !assistantState.isLoadingSessions;

    return Scaffold(
      backgroundColor: _assistantBg,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        title: const Text('Ask Neatie'),
        actions: [
          IconButton(
            tooltip: assistantState.thinkingMode
                ? 'Thinking on (Minimax)'
                : 'Thinking off (RNJ fast mode)',
            onPressed: assistantState.isSending
                ? null
                : () => ref.read(assistantProvider.notifier).toggleThinkingMode(),
            icon: Icon(
              assistantState.thinkingMode
                  ? Icons.psychology_alt_rounded
                  : Icons.flash_on_rounded,
            ),
          ),
          IconButton(
            tooltip: 'Chats',
            onPressed: () => _openSessionsSheet(),
            icon: const Icon(Icons.history_rounded),
          ),
          IconButton(
            tooltip: 'New chat',
            onPressed: assistantState.isSending
                ? null
                : () => ref.read(assistantProvider.notifier).clearConversation(),
            icon: const Icon(Icons.add_comment_rounded),
          ),
        ],
      ),
      body: SafeArea(
        child: AnimatedSwitcher(
          duration: const Duration(milliseconds: 320),
          switchInCurve: Curves.easeOutCubic,
          switchOutCurve: Curves.easeInCubic,
          transitionBuilder: (child, animation) {
            return FadeTransition(
              opacity: animation,
              child: SlideTransition(
                position: Tween<Offset>(
                  begin: const Offset(0, 0.03),
                  end: Offset.zero,
                ).animate(animation),
                child: child,
              ),
            );
          },
          child: assistantState.isLoadingSessions && assistantState.messages.isEmpty
              ? Center(
                  key: const ValueKey('assistant-loading'),
                  child: _buildStatusCard('Loading chats…'),
                )
              : isFreshSession
                  ? _buildFreshSession(assistantState)
                  : _buildChatList(assistantState),
        ),
      ),
    );
  }
}

class _SessionSectionHeader extends StatelessWidget {
  final String title;

  const _SessionSectionHeader({required this.title});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 10, 4, 8),
      child: Align(
        alignment: Alignment.centerLeft,
        child: Text(
          title,
          style: TextStyle(
            color: Colors.white.withValues(alpha: 0.62),
            fontSize: 12,
            fontWeight: FontWeight.w700,
            letterSpacing: 0.4,
          ),
        ),
      ),
    );
  }
}

class _SessionTile extends StatelessWidget {
  final AssistantSessionSummary session;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback onRename;
  final VoidCallback onTogglePin;
  final VoidCallback onToggleArchive;
  final VoidCallback onDelete;

  const _SessionTile({
    required this.session,
    required this.selected,
    required this.onTap,
    required this.onRename,
    required this.onTogglePin,
    required this.onToggleArchive,
    required this.onDelete,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      type: MaterialType.transparency,
      child: ListTile(
        selected: selected,
        selectedTileColor: Colors.white.withValues(alpha: 0.05),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
        ),
        leading: Icon(
          session.isArchived
              ? Icons.archive_rounded
              : session.isPinned
                  ? Icons.push_pin_rounded
                  : Icons.chat_bubble_outline_rounded,
          color: session.isArchived ? Colors.white54 : Colors.white70,
        ),
        title: Text(
          session.title,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(color: Colors.white),
        ),
        subtitle: session.lastMessagePreview.trim().isEmpty
            ? null
            : Text(
                session.lastMessagePreview,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: Colors.white.withValues(alpha: 0.58),
                ),
              ),
        onTap: onTap,
        trailing: PopupMenuButton<String>(
          color: _assistantSurface,
          iconColor: Colors.white70,
          onSelected: (value) {
            switch (value) {
              case 'rename':
                onRename();
                break;
              case 'pin':
                onTogglePin();
                break;
              case 'archive':
                onToggleArchive();
                break;
              case 'delete':
                onDelete();
                break;
            }
          },
          itemBuilder: (context) => [
            const PopupMenuItem<String>(
              value: 'rename',
              child: Text('Rename'),
            ),
            PopupMenuItem<String>(
              value: 'pin',
              child: Text(session.isPinned ? 'Unpin' : 'Pin'),
            ),
            PopupMenuItem<String>(
              value: 'archive',
              child: Text(session.isArchived ? 'Unarchive' : 'Archive'),
            ),
            const PopupMenuItem<String>(
              value: 'delete',
              child: Text('Delete'),
            ),
          ],
        ),
      ),
    );
  }
}

class _AssistantTextPromptPage extends StatefulWidget {
  final String title;
  final String initialValue;
  final String confirmLabel;

  const _AssistantTextPromptPage({
    required this.title,
    required this.initialValue,
    required this.confirmLabel,
  });

  @override
  State<_AssistantTextPromptPage> createState() => _AssistantTextPromptPageState();
}

class _AssistantTextPromptPageState extends State<_AssistantTextPromptPage> {
  late final TextEditingController _controller;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: widget.initialValue);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _submit() {
    final trimmed = _controller.text.trim();
    Navigator.of(context).pop(trimmed.isEmpty ? null : trimmed);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _assistantBg,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        title: Text(widget.title),
        actions: [
          TextButton(
            onPressed: _submit,
            child: Text(
              widget.confirmLabel,
              style: const TextStyle(color: Colors.white),
            ),
          ),
        ],
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 18, 20, 20),
          child: TextField(
            controller: _controller,
            autofocus: true,
            style: const TextStyle(color: Colors.white),
            minLines: 1,
            maxLines: 8,
            textInputAction: TextInputAction.done,
            onSubmitted: (_) => _submit(),
            decoration: InputDecoration(
              filled: true,
              fillColor: _assistantSurface,
              hintText: '',
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(18),
                borderSide: BorderSide.none,
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _ComposerPanel extends StatelessWidget {
  final TextEditingController controller;
  final bool isSending;
  final bool showInlinePlayer;
  final Future<void> Function() onSend;
  final VoidCallback? onOpenPlayer;

  const _ComposerPanel({
    required this.controller,
    required this.isSending,
    required this.showInlinePlayer,
    required this.onSend,
    this.onOpenPlayer,
  });

  @override
  Widget build(BuildContext context) {
    return NeatieSurface(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
      radius: 0,
      color: _assistantSurfaceAlt,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          _AssistantInlinePlayer(onOpenPlayer: onOpenPlayer),
          if (showInlinePlayer) const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: TextField(
                  controller: controller,
                  style: const TextStyle(color: Colors.white),
                  minLines: 1,
                  maxLines: 5,
                  textInputAction: TextInputAction.send,
                  onSubmitted: (_) => onSend(),
                  decoration: InputDecoration(
                    hintText: '',
                    filled: true,
                    fillColor: _assistantSurface,
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(16),
                      borderSide: BorderSide.none,
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              FilledButton(
                style: FilledButton.styleFrom(
                  backgroundColor: Colors.white,
                  foregroundColor: Colors.black,
                  padding: const EdgeInsets.symmetric(
                    horizontal: 18,
                    vertical: 18,
                  ),
                  shape: const CircleBorder(),
                ),
                onPressed: isSending ? null : () => onSend(),
                child: const Icon(Icons.arrow_upward_rounded),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _FreshComposerPanel extends StatelessWidget {
  final TextEditingController controller;
  final bool isSending;
  final Future<void> Function() onSend;

  const _FreshComposerPanel({
    required this.controller,
    required this.isSending,
    required this.onSend,
  });

  @override
  Widget build(BuildContext context) {
    return NeatieSurface(
      constraints: const BoxConstraints(maxWidth: 760),
      padding: const EdgeInsets.all(12),
      radius: 28,
      color: _assistantSurfaceAlt,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Expanded(
            child: TextField(
              controller: controller,
              style: const TextStyle(color: Colors.white, fontSize: 18),
              minLines: 2,
              maxLines: 6,
              textInputAction: TextInputAction.send,
              onSubmitted: (_) => onSend(),
              decoration: const InputDecoration(
                hintText: '',
                border: InputBorder.none,
                contentPadding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              ),
            ),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: Colors.white,
              foregroundColor: Colors.black,
              padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 16),
              shape: const CircleBorder(),
            ),
            onPressed: isSending ? null : () => onSend(),
            child: const Icon(Icons.arrow_upward_rounded),
          ),
        ],
      ),
    );
  }
}

class _MetadataChip extends StatelessWidget {
  final String label;
  final VoidCallback? onTap;

  const _MetadataChip({
    required this.label,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final chip = NeatiePill(label: label);
    if (onTap == null) {
      return chip;
    }
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(999),
      child: chip,
    );
  }
}

class _AssistantFactCard extends StatelessWidget {
  final AssistantFactCard fact;
  final VoidCallback? onOpenSource;

  const _AssistantFactCard({
    required this.fact,
    this.onOpenSource,
  });

  @override
  Widget build(BuildContext context) {
    return NeatieSurface(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(12),
      radius: neatieRadiusMedium,
      color: Colors.white.withValues(alpha: 0.035),
      blur: false,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            fact.title,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 15,
              fontWeight: FontWeight.w700,
            ),
          ),
          if (fact.subtitle.trim().isNotEmpty) ...[
            const SizedBox(height: 4),
            Text(
              fact.subtitle,
              style: TextStyle(
                color: Colors.white.withValues(alpha: 0.62),
                fontSize: 12,
              ),
            ),
          ],
          if (fact.value.trim().isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(
              fact.value,
              style: const TextStyle(
                color: Colors.white,
                fontSize: 14,
                height: 1.35,
              ),
            ),
          ],
          if (fact.metadata.isNotEmpty) ...[
            const SizedBox(height: 10),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: fact.metadata
                  .map((entry) => _MetadataChip(label: entry))
                  .toList(growable: false),
            ),
          ],
          if (onOpenSource != null &&
              (fact.sourceLabel ?? '').trim().isNotEmpty) ...[
            const SizedBox(height: 10),
            _ActionChip(
              icon: Icons.open_in_new_rounded,
              label: fact.sourceLabel!,
              onTap: onOpenSource!,
            ),
          ],
        ],
      ),
    );
  }
}

class _TinyIconButton extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;

  const _TinyIconButton({
    required this.icon,
    required this.tooltip,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(999),
        child: Padding(
          padding: const EdgeInsets.all(4),
          child: Icon(
            icon,
            size: 16,
            color: Colors.white.withValues(alpha: 0.74),
          ),
        ),
      ),
    );
  }
}

class _ActionChip extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  const _ActionChip({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(999),
      child: NeatieSurface(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        radius: 999,
        color: Colors.white.withValues(alpha: 0.04),
        blur: false,
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 16, color: Colors.white),
            const SizedBox(width: 6),
            Text(
              label,
              style: const TextStyle(
                color: Colors.white,
                fontSize: 12,
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _IconActionButton extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;

  const _IconActionButton({
    required this.icon,
    required this.tooltip,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(999),
        child: Container(
          width: 38,
          height: 38,
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.06),
            borderRadius: BorderRadius.circular(999),
            border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
          ),
          child: Icon(icon, size: 18, color: Colors.white),
        ),
      ),
    );
  }
}

class _AssistantInlinePlayer extends ConsumerWidget {
  final VoidCallback? onOpenPlayer;

  const _AssistantInlinePlayer({this.onOpenPlayer});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final playerState = ref.watch(audioPlayerProvider);
    final audioNotifier = ref.read(audioPlayerProvider.notifier);
    if (playerState.currentTrackName == 'No track loaded') {
      return const SizedBox.shrink();
    }

    final bar = NeatieSurface(
      height: 58,
      radius: neatieRadiusMedium,
      color: Colors.white.withValues(alpha: 0.04),
      blur: false,
      child: Column(
        children: [
          Expanded(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14),
              child: Row(
                children: [
                  Expanded(
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          playerState.currentTrackName,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 14,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                        const SizedBox(height: 2),
                        Text(
                          playerState.artist ?? '',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: Colors.white.withValues(alpha: 0.58),
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 10),
                  InkWell(
                    onTap: () {
                      unawaited(
                        playerState.isPlaying
                            ? audioNotifier.pause()
                            : audioNotifier.play(),
                      );
                    },
                    borderRadius: BorderRadius.circular(999),
                    child: Container(
                      width: 36,
                      height: 36,
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.08),
                        shape: BoxShape.circle,
                      ),
                      child: Icon(
                        playerState.isPlaying
                            ? Icons.pause_rounded
                            : Icons.play_arrow_rounded,
                        color: Colors.white,
                        size: 22,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
          if (playerState.duration > 0)
            ClipRRect(
              borderRadius: const BorderRadius.only(
                bottomLeft: Radius.circular(12),
                bottomRight: Radius.circular(12),
              ),
              child: LinearProgressIndicator(
                value: (playerState.currentPosition / playerState.duration)
                    .clamp(0.0, 1.0),
                backgroundColor: Colors.transparent,
                color: _assistantAccent,
                minHeight: 2,
              ),
            ),
        ],
      ),
    );

    if (onOpenPlayer == null) {
      return bar;
    }
    return GestureDetector(
      onTap: onOpenPlayer,
      child: bar,
    );
  }
}

class _PlaylistDraftCard extends StatelessWidget {
  final AssistantPlaylistDraft draft;
  final VoidCallback onPlayNow;
  final VoidCallback onCreate;
  final VoidCallback onAddToExisting;

  const _PlaylistDraftCard({
    required this.draft,
    required this.onPlayNow,
    required this.onCreate,
    required this.onAddToExisting,
  });

  @override
  Widget build(BuildContext context) {
    return NeatieSurface(
      padding: const EdgeInsets.all(12),
      radius: neatieRadiusMedium,
      color: Colors.white.withValues(alpha: 0.035),
      blur: false,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            draft.name,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            _sanitizeAssistantText(draft.summary),
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.7),
              height: 1.4,
            ),
          ),
          const SizedBox(height: 10),
          Text(
            '${draft.tracks.length} tracks ready',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.58),
              fontSize: 12,
            ),
          ),
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _ActionChip(
                icon: Icons.play_arrow_rounded,
                label: 'Play now',
                onTap: onPlayNow,
              ),
              _ActionChip(
                icon: Icons.library_add_rounded,
                label: 'Create playlist',
                onTap: onCreate,
              ),
              _ActionChip(
                icon: Icons.playlist_add_rounded,
                label: 'Add to existing',
                onTap: onAddToExisting,
              ),
            ],
          ),
        ],
      ),
    );
  }
}

String _sanitizeAssistantText(String value) {
  var text = value;
  text = text.replaceAllMapped(
    RegExp(r'\*\*(.*?)\*\*', dotAll: true),
    (match) => match.group(1) ?? '',
  );
  text = text.replaceAllMapped(
    RegExp(r'__(.*?)__', dotAll: true),
    (match) => match.group(1) ?? '',
  );
  text = text.replaceAll('`', '');
  text = text.replaceAllMapped(
    RegExp(r'^\s{0,3}#{1,6}\s*', multiLine: true),
    (_) => '',
  );
  return text;
}

String _formatDuration(int seconds) {
  final minutes = seconds ~/ 60;
  final remainingSeconds = seconds % 60;
  return '$minutes:${remainingSeconds.toString().padLeft(2, '0')}';
}

int _parseAssistantDuration(dynamic value) {
  if (value == null) return 0;
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}
