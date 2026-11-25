/**
 * Copyright 2026 Google LLC
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

import {Injectable, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {firstValueFrom} from 'rxjs';
import {environment} from '../../../environments/environment';

export interface AssistantChatMessage {
  role: 'user' | 'assistant';
  content: string;
  modelUsed?: string;
}

export interface StudioAssistantResponse {
  answer: string;
  modelUsed: string;
}

export type HelpDrawerTab = 'guide' | 'assistant';

@Injectable({
  providedIn: 'root',
})
export class StudioAssistantService {
  readonly isOpen = signal<boolean>(false);
  readonly activeTab = signal<HelpDrawerTab>('guide');
  readonly isLoading = signal<boolean>(false);
  readonly errorMessage = signal<string | null>(null);
  readonly messages = signal<AssistantChatMessage[]>([
    {
      role: 'assistant',
      content:
        'Hi! I am your **Creative Studio & GMP Guide** (powered by Gemini 3 Flash). Ask me anything about GMP models, prompt tags, 4K upscaling, or settings in your current tab.',
    },
  ]);

  constructor(private readonly http: HttpClient) {}

  openDrawer(tab: HelpDrawerTab = 'guide'): void {
    this.activeTab.set(tab);
    this.isOpen.set(true);
  }

  closeDrawer(): void {
    this.isOpen.set(false);
  }

  toggleDrawer(): void {
    this.isOpen.update(open => !open);
  }

  clearConversation(): void {
    this.errorMessage.set(null);
    this.messages.set([
      {
        role: 'assistant',
        content:
          'Conversation cleared. Ask me anything about Creative Studio or GMP features!',
      },
    ]);
  }

  async askQuestion(
    question: string,
    context: {currentRoute: string; activeModel?: string; activeMode?: string},
  ): Promise<void> {
    const trimmed = question.trim();
    if (!trimmed || this.isLoading()) {
      return;
    }

    const priorHistory = this.messages()
      .slice(-10)
      .map(m => ({role: m.role, content: m.content}));

    this.messages.update(list => [...list, {role: 'user', content: trimmed}]);
    this.isLoading.set(true);
    this.errorMessage.set(null);

    try {
      const response = await firstValueFrom(
        this.http.post<StudioAssistantResponse>(
          `${environment.backendURL}/gemini/assistant`,
          {
            question: trimmed,
            currentRoute: context.currentRoute,
            activeModel: context.activeModel || '',
            activeMode: context.activeMode || '',
            history: priorHistory,
          },
        ),
      );
      this.messages.update(list => [
        ...list,
        {
          role: 'assistant',
          content: response.answer,
          modelUsed: response.modelUsed,
        },
      ]);
    } catch (error: unknown) {
      console.error('StudioAssistantService error:', error);
      const fallbackMsg =
        'Unable to reach the Gemini assistant right now. Check the Guide tab for instant offline reference or try again.';
      this.errorMessage.set(fallbackMsg);
      this.messages.update(list => [
        ...list,
        {role: 'assistant', content: fallbackMsg},
      ]);
    } finally {
      this.isLoading.set(false);
    }
  }
}
