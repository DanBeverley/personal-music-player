import 'package:flutter/material.dart';

import 'web_auth.dart';
import 'design_system.dart';

class AccountScreen extends StatefulWidget {
  const AccountScreen({super.key, required this.auth});

  final WebAuth auth;

  @override
  State<AccountScreen> createState() => _AccountScreenState();
}

class _AccountScreenState extends State<AccountScreen> {
  final _formKey = GlobalKey<FormState>();
  final _email = TextEditingController();
  final _password = TextEditingController();
  final _newPassword = TextEditingController();
  bool _signUp = false;
  String? _feedback;

  WebAuth get auth => widget.auth;

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    _newPassword.dispose();
    super.dispose();
  }

  String? _emailError(String? value) {
    final email = value?.trim() ?? '';
    if (email.isEmpty) return 'Enter your email address.';
    if (!RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(email)) {
      return 'Enter a valid email address.';
    }
    return null;
  }

  String? _passwordError(String? value) =>
      (value ?? '').length < 6 ? 'Use at least 6 characters.' : null;

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate() || auth.busy) return;
    setState(() => _feedback = null);
    if (_signUp) {
      await auth.signUp(_email.text, _password.text);
      if (mounted && auth.error == null) {
        setState(() => _feedback = 'Check your email to confirm your account.');
      }
    } else {
      await auth.signIn(_email.text, _password.text);
    }
  }

  Future<void> _resetPassword() async {
    if (auth.busy) return;
    final error = _emailError(_email.text);
    if (error != null) {
      setState(() => _feedback = error);
      return;
    }
    setState(() => _feedback = null);
    await auth.resetPassword(_email.text);
    if (mounted && auth.error == null) {
      setState(() => _feedback = 'Check your email for a password reset link.');
    }
  }

  Future<void> _updatePassword() async {
    if (auth.busy) return;
    if (_passwordError(_newPassword.text) != null) {
      setState(() => _feedback = 'Use at least 6 characters.');
      return;
    }
    setState(() => _feedback = null);
    await auth.updatePassword(_newPassword.text);
    if (mounted && auth.error == null) {
      setState(() => _feedback = 'Your password has been updated.');
    }
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: auth,
        builder: (context, _) => _buildContent(context),
      );

  Widget _buildContent(BuildContext context) {
    final recovery = auth.passwordRecovery;
    final signedIn = auth.email != null && !recovery;
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: EditorialSurface(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: signedIn
                  ? _signedInView()
                  : recovery
                      ? _recoveryView()
                      : _authView(),
            ),
          ),
        ),
      ),
    );
  }

  Widget _signedInView() => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Your space.', style: NeatieDesign.display(36)),
          const SizedBox(height: 12),
          Text(auth.email!),
          const SizedBox(height: 24),
          _errorText(),
          FilledButton(
              onPressed: auth.busy ? null : auth.signOut,
              child: const Text('Sign out')),
        ],
      );

  Widget _recoveryView() => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text('Set a new password',
              style: TextStyle(fontSize: 24, fontWeight: FontWeight.bold)),
          const SizedBox(height: 16),
          TextFormField(
              controller: _newPassword,
              obscureText: true,
              enabled: !auth.busy,
              decoration: const InputDecoration(labelText: 'New password'),
              validator: _passwordError),
          const SizedBox(height: 16),
          _errorText(),
          _feedbackText(),
          FilledButton(
              onPressed: auth.busy ? null : _updatePassword,
              child: const Text('Update password')),
        ],
      );

  Widget _authView() => Form(
        key: _formKey,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(_signUp ? 'Make it yours.' : 'Welcome back.',
                style: NeatieDesign.display(36)),
            const SizedBox(height: 10),
            Text(_signUp
                ? 'Create your Neatie account.'
                : 'Sign in to your Neatie account.'),
            const SizedBox(height: 16),
            TextFormField(
                controller: _email,
                enabled: !auth.busy,
                keyboardType: TextInputType.emailAddress,
                decoration: const InputDecoration(labelText: 'Email'),
                validator: _emailError),
            const SizedBox(height: 12),
            TextFormField(
                controller: _password,
                enabled: !auth.busy,
                obscureText: true,
                decoration: const InputDecoration(labelText: 'Password'),
                validator: _passwordError),
            const SizedBox(height: 16),
            _errorText(),
            _feedbackText(),
            FilledButton(
                onPressed: auth.busy ? null : _submit,
                child: Text(_signUp ? 'Sign up' : 'Sign in')),
            TextButton(
                onPressed:
                    auth.busy ? null : () => setState(() => _signUp = !_signUp),
                child: Text(_signUp
                    ? 'Already have an account? Sign in'
                    : 'Need an account? Sign up')),
            TextButton(
                onPressed: auth.busy ? null : _resetPassword,
                child: const Text('Reset password')),
            const Divider(height: 28),
            OutlinedButton(
                onPressed: auth.busy ? null : auth.signInWithGoogle,
                child: const Text('Continue with Google')),
            const SizedBox(height: 8),
            if (const bool.fromEnvironment('WEB_GITHUB_LOGIN'))
              OutlinedButton(
                  onPressed: auth.busy ? null : auth.signInWithGitHub,
                  child: const Text('Continue with GitHub')),
          ],
        ),
      );

  Widget _errorText() => auth.error == null
      ? const SizedBox.shrink()
      : Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: Text(auth.error!,
              style: TextStyle(color: Theme.of(context).colorScheme.error)));

  Widget _feedbackText() => _feedback == null
      ? const SizedBox.shrink()
      : Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: Text(_feedback!,
              style: TextStyle(color: Theme.of(context).colorScheme.primary)));
}
