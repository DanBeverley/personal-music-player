import 'package:flutter/foundation.dart';

@immutable
class HomeScreenControllerState {
  final int focusSearchRequestId;
  final int showHomeFeedRequestId;
  final int handleBackRequestId;
  final int openSongMatchRequestId;
  final bool preferPendingSharedForSongMatch;
  final bool canHandleBack;

  const HomeScreenControllerState({
    this.focusSearchRequestId = 0,
    this.showHomeFeedRequestId = 0,
    this.handleBackRequestId = 0,
    this.openSongMatchRequestId = 0,
    this.preferPendingSharedForSongMatch = false,
    this.canHandleBack = false,
  });

  HomeScreenControllerState copyWith({
    int? focusSearchRequestId,
    int? showHomeFeedRequestId,
    int? handleBackRequestId,
    int? openSongMatchRequestId,
    bool? preferPendingSharedForSongMatch,
    bool? canHandleBack,
  }) {
    return HomeScreenControllerState(
      focusSearchRequestId: focusSearchRequestId ?? this.focusSearchRequestId,
      showHomeFeedRequestId:
          showHomeFeedRequestId ?? this.showHomeFeedRequestId,
      handleBackRequestId: handleBackRequestId ?? this.handleBackRequestId,
      openSongMatchRequestId:
          openSongMatchRequestId ?? this.openSongMatchRequestId,
      preferPendingSharedForSongMatch:
          preferPendingSharedForSongMatch ??
              this.preferPendingSharedForSongMatch,
      canHandleBack: canHandleBack ?? this.canHandleBack,
    );
  }
}

class HomeScreenController extends ValueNotifier<HomeScreenControllerState> {
  HomeScreenController() : super(const HomeScreenControllerState());

  void requestFocusSearch() {
    value = value.copyWith(
      focusSearchRequestId: value.focusSearchRequestId + 1,
    );
  }

  void requestShowHomeFeed() {
    value = value.copyWith(
      showHomeFeedRequestId: value.showHomeFeedRequestId + 1,
    );
  }

  void requestOpenSongMatchSheet({
    bool preferPendingShared = false,
  }) {
    value = value.copyWith(
      openSongMatchRequestId: value.openSongMatchRequestId + 1,
      preferPendingSharedForSongMatch: preferPendingShared,
    );
  }

  void requestHandleBack() {
    value = value.copyWith(
      handleBackRequestId: value.handleBackRequestId + 1,
    );
  }

  void updateCanHandleBack(bool nextValue) {
    if (value.canHandleBack == nextValue) return;
    value = value.copyWith(canHandleBack: nextValue);
  }
}
