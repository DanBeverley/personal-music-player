import 'package:flutter/material.dart';

abstract final class NeatieDesign {
  static const background = Color(0xff0d1010);
  static const surface = Color(0xff171c1b);
  static const raised = Color(0xff202725);
  static const ink = Color(0xfff3f0e8);
  static const muted = Color(0xffa6b2aa);
  static const accent = Color(0xffceddbb);
  static const line = Color(0xff2b3430);

  static TextStyle display(double size) => TextStyle(
      fontFamily: 'NeatieDisplay',
      fontSize: size,
      height: 1.08,
      letterSpacing: -.8,
      color: ink,
      fontWeight: FontWeight.w400);
  static const eyebrow = TextStyle(
      fontSize: 10,
      fontWeight: FontWeight.w700,
      letterSpacing: 2.2,
      color: muted);

  static ThemeData get theme {
    final base = ThemeData(
        brightness: Brightness.dark,
        useMaterial3: true,
        fontFamily: 'NeatieSans',
        scaffoldBackgroundColor: background,
        colorScheme: const ColorScheme.dark(
            primary: accent,
            onPrimary: background,
            secondary: accent,
            surface: surface,
            onSurface: ink,
            onSurfaceVariant: muted,
            outline: line));
    return base.copyWith(
      textTheme: base.textTheme.copyWith(
          headlineLarge: display(48),
          headlineMedium: display(38),
          headlineSmall: display(30),
          titleLarge: const TextStyle(
              fontFamily: 'NeatieSans',
              fontSize: 23,
              color: ink,
              fontWeight: FontWeight.w700,
              letterSpacing: -.6),
          titleMedium: const TextStyle(
              fontFamily: 'NeatieSans',
              fontSize: 16,
              color: ink,
              fontWeight: FontWeight.w600),
          titleSmall: const TextStyle(
              fontFamily: 'NeatieSans',
              fontSize: 13,
              color: ink,
              fontWeight: FontWeight.w600),
          bodyMedium: const TextStyle(
              fontFamily: 'NeatieSans',
              fontSize: 13,
              color: muted,
              height: 1.5),
          bodySmall: const TextStyle(
              fontFamily: 'NeatieSans',
              fontSize: 11,
              color: muted,
              height: 1.5)),
      appBarTheme: const AppBarTheme(
          backgroundColor: background,
          surfaceTintColor: Colors.transparent,
          elevation: 0,
          centerTitle: false),
      cardTheme: CardThemeData(
          color: surface,
          elevation: 0,
          margin: const EdgeInsets.all(4),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(18))),
      dividerTheme: const DividerThemeData(color: line, thickness: 1),
      listTileTheme: const ListTileThemeData(
          contentPadding: EdgeInsets.symmetric(horizontal: 14, vertical: 4),
          iconColor: muted,
          selectedColor: accent,
          selectedTileColor: raised),
      inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: surface,
          contentPadding:
              const EdgeInsets.symmetric(horizontal: 20, vertical: 20),
          border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(16),
              borderSide: const BorderSide(color: line)),
          enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(16),
              borderSide: const BorderSide(color: line)),
          focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(16),
              borderSide: const BorderSide(color: accent))),
      filledButtonTheme: FilledButtonThemeData(
          style: FilledButton.styleFrom(
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 20),
              textStyle: const TextStyle(
                  fontFamily: 'NeatieSans', fontWeight: FontWeight.w700),
              shape: const StadiumBorder())),
      outlinedButtonTheme: OutlinedButtonThemeData(
          style: OutlinedButton.styleFrom(
              foregroundColor: ink,
              side: const BorderSide(color: line),
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 18),
              shape: const StadiumBorder())),
      chipTheme: base.chipTheme.copyWith(
          backgroundColor: surface,
          selectedColor: raised,
          side: const BorderSide(color: line),
          shape: const StadiumBorder(),
          showCheckmark: false,
          labelStyle: const TextStyle(
              fontFamily: 'NeatieSans', fontSize: 12, color: ink)),
      sliderTheme: base.sliderTheme.copyWith(
          trackHeight: 3,
          thumbShape: const RoundSliderThumbShape(enabledThumbRadius: 5),
          overlayShape: const RoundSliderOverlayShape(overlayRadius: 12),
          activeTrackColor: accent,
          inactiveTrackColor: line),
      navigationBarTheme: const NavigationBarThemeData(
          backgroundColor: background,
          indicatorColor: raised,
          height: 70,
          labelTextStyle: WidgetStatePropertyAll(TextStyle(
              fontFamily: 'NeatieSans',
              fontSize: 10,
              fontWeight: FontWeight.w600))),
    );
  }
}

class NeatieBrand extends StatelessWidget {
  const NeatieBrand({super.key, this.size = 30});
  final double size;
  @override
  Widget build(BuildContext context) =>
      Row(mainAxisSize: MainAxisSize.min, children: [
        Container(
            width: 30,
            height: 30,
            decoration: const BoxDecoration(
                shape: BoxShape.circle, color: NeatieDesign.accent),
            child: const Icon(Icons.graphic_eq_rounded,
                size: 19, color: NeatieDesign.background)),
        const SizedBox(width: 10),
        Text('neatie', style: NeatieDesign.display(size)),
      ]);
}

class EditorialHeading extends StatelessWidget {
  const EditorialHeading(
      {super.key,
      required this.title,
      required this.subtitle,
      required this.eyebrow,
      this.action});
  final String title, subtitle, eyebrow;
  final Widget? action;
  @override
  Widget build(BuildContext context) =>
      LayoutBuilder(builder: (context, bounds) {
        final compact = bounds.maxWidth < 600;
        return Padding(
            padding: EdgeInsets.only(top: compact ? 12 : 24, bottom: 24),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Expanded(
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                    Text(eyebrow, style: NeatieDesign.eyebrow),
                    const SizedBox(height: 12),
                    Text(title, style: NeatieDesign.display(compact ? 34 : 46)),
                    const SizedBox(height: 12),
                    Text(subtitle,
                        style: Theme.of(context).textTheme.bodyMedium),
                  ])),
              if (action != null) ...[const SizedBox(width: 12), action!],
            ]));
      });
}

class EditorialSurface extends StatefulWidget {
  const EditorialSurface(
      {super.key,
      required this.child,
      this.radius = 20,
      this.color = NeatieDesign.surface});
  final Widget child;
  final double radius;
  final Color color;
  @override
  State<EditorialSurface> createState() => _EditorialSurfaceState();
}

class _EditorialSurfaceState extends State<EditorialSurface> {
  bool hovered = false;
  @override
  Widget build(BuildContext context) => MouseRegion(
      onEnter: (_) => setState(() => hovered = true),
      onExit: (_) => setState(() => hovered = false),
      child: AnimatedContainer(
          duration: MediaQuery.disableAnimationsOf(context)
              ? Duration.zero
              : const Duration(milliseconds: 180),
          decoration: BoxDecoration(
              color: hovered ? NeatieDesign.raised : widget.color,
              borderRadius: BorderRadius.circular(widget.radius),
              border: Border.all(
                  color:
                      hovered ? const Color(0xff465548) : NeatieDesign.line)),
          clipBehavior: Clip.antiAlias,
          child: widget.child));
}
