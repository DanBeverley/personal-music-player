const List<String> sharedTasteKeywords = <String>[
  'rock',
  'metal',
  'jazz',
  'blues',
  'classical',
  'orchestra',
  'punk',
  'indie',
  'folk',
  'country',
  'ambient',
  'chill',
  'sad',
  'happy',
  'workout',
  'gym',
  'sleep',
  'focus',
  'lofi',
  'edm',
  'house',
  'techno',
  'trance',
  'disco',
  'pop',
  'rap',
  'hip hop',
  'rnb',
  'soul',
  'funk',
  'vibe',
  'mood',
  'party',
  'romantic',
  'night',
  'morning',
  'road trip',
  '80s',
  '90s',
  '2000s',
];

void bumpSearchSignalWeight(
  Map<String, double> weights,
  String? value,
  double weight,
) {
  final normalized = value?.trim().toLowerCase();
  if (normalized == null || normalized.isEmpty) return;
  weights.update(
    normalized,
    (current) => current + weight,
    ifAbsent: () => weight,
  );
}

String? extractSearchArtistHint(dynamic rawTrack) {
  if (rawTrack is! Map) return null;
  return rawTrack['channel']?.toString() ??
      rawTrack['author']?.toString() ??
      rawTrack['artist']?.toString();
}

String? extractSearchAlbumHint(dynamic rawTrack) {
  if (rawTrack is! Map) return null;
  return rawTrack['album']?.toString() ?? rawTrack['album_title']?.toString();
}

bool looksLikeSemanticTasteQuery(String query) {
  final normalized = query.trim().toLowerCase();
  if (normalized.isEmpty) return false;
  if (normalized.length > 38) return true;
  if (normalized.contains(' mix') ||
      normalized.contains(' playlist') ||
      normalized.contains(' songs') ||
      normalized.contains(' music')) {
    return true;
  }
  return sharedTasteKeywords.any((keyword) {
    if (keyword.contains(' ')) {
      return normalized.contains(keyword);
    }
    return RegExp(
      '(^|\\s)${RegExp.escape(keyword)}(\\s|\$)',
    ).hasMatch(normalized);
  });
}

bool isMetadataHeavyQuery(String query) {
  final normalized = query.trim().toLowerCase();
  if (normalized.isEmpty) return false;
  const noisyPhrases = <String>[
    'deluxe remastered',
    'deluxe edition',
    'original soundtrack',
    'motion picture',
    'tribute to',
    'karaoke version',
    'radio edit',
  ];
  if (noisyPhrases.any(normalized.contains)) {
    return true;
  }
  const noisyTokens = <String>{
    'bonus',
    'deluxe',
    'edition',
    'karaoke',
    'mono',
    'original',
    'remaster',
    'remastered',
    'soundtrack',
    'stereo',
    'tribute',
    'version',
  };
  final hits =
      normalized.split(RegExp(r'\s+')).where(noisyTokens.contains).length;
  return hits >= 2;
}

String classifySearchQueryMode(String query) {
  final normalized = query.trim().toLowerCase();
  if (normalized.isEmpty) return 'exact';
  if (looksLikeSemanticTasteQuery(normalized)) {
    return 'taste';
  }
  const entityHints = <String>[
    'album',
    'artist',
    'band',
    'discography',
    'ost',
    'soundtrack',
  ];
  if (entityHints.any((hint) => normalized.contains(hint))) {
    return 'entity';
  }
  return 'exact';
}
