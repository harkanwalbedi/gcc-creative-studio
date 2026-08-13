/**
 * Copyright 2025 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

export const environment = {
  firebase: {
    apiKey: 'dummy-local-api-key',
    authDomain: 'vertex-test-project-383415.firebaseapp.com',
    projectId: 'vertex-test-project-383415',
    storageBucket: 'vertex-test-project-383415.firebasestorage.app',
    messagingSenderId: '58707440259',
    appId: '1:58707440259:web:localdev',
    measurementId: 'G-LOCALDEV',
  },
  production: false,
  isLocal: true,
  backendURL: '/api',
  EMAIL_REGEX:
    /^(([^<>()[\]\\.,;:\s@"]+(\.[^<>()[\]\\.,;:\s@"]+)*)|(".+"))@((\[\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\])|(([a-zA-Z\-0-9]+\.)+[a-zA-Z]{2,}))$/,
  ADMIN: 'admin',
  GOOGLE_CLIENT_ID: 'dummy-google-client-id',
};
