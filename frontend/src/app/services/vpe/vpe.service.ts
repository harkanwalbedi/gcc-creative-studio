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

import {HttpClient} from '@angular/common/http';
import {Injectable} from '@angular/core';
import {MatSnackBar} from '@angular/material/snack-bar';
import {
  BehaviorSubject,
  catchError,
  EMPTY,
  Observable,
  Subscription,
  switchMap,
  tap,
  timer,
} from 'rxjs';
import {environment} from '../../../environments/environment';
import {JobStatus, MediaItem} from '../../common/models/media-item.model';
import {
  UpscaleVideoDto,
  VpeScreeningResponse,
} from '../../common/models/vpe.model';
import {
  handleErrorSnackbar,
  handleInfoSnackbar,
  handleSuccessSnackbar,
} from '../../utils/handleMessageSnackbar';

/**
 * Own service rather than more methods on SearchService, mirroring the
 * backend's own router and thread pool: VPE is an allowlisted capability
 * with a job that runs an order of magnitude longer than anything else in
 * the app, and keeping it separate means it can be found (or gated) as one
 * unit rather than scattered across the general video service.
 */
@Injectable({
  providedIn: 'root',
})
export class VpeService {
  private activeUpscaleJob = new BehaviorSubject<MediaItem | null>(null);
  public activeUpscaleJob$ = this.activeUpscaleJob.asObservable();
  private upscalePollingSubscription: Subscription | null = null;

  constructor(
    private http: HttpClient,
    private _snackBar: MatSnackBar,
  ) {}

  private isJobFinished(item: MediaItem): boolean {
    return (
      item.status === JobStatus.COMPLETED ||
      item.status === JobStatus.FAILED ||
      item.status === JobStatus.STOPPED
    );
  }

  /**
   * Cheaply checks whether a stored gallery video is worth offering the
   * upscale action on. Can only rule the capability out, never confirm it -
   * the row carries no frame rate and stores resolution as a name rather
   * than pixels - so `offer: true` still means "not yet ruled out", not
   * "guaranteed to work".
   */
  screenForUpscale(mediaItemId: number): Observable<VpeScreeningResponse> {
    const url = `${environment.backendURL}/videos/vpe/screening/${mediaItemId}`;
    return this.http.get<VpeScreeningResponse>(url);
  }

  /**
   * Queues an upscale job and starts polling its placeholder row for
   * completion, the same way SearchService tracks a Veo generation.
   */
  startUpscale(payload: UpscaleVideoDto): Observable<MediaItem> {
    const url = `${environment.backendURL}/videos/vpe/upscale`;
    return this.http.post<MediaItem>(url, payload).pipe(
      tap(initialItem => {
        this.activeUpscaleJob.next(initialItem);
        this.startUpscalePolling(initialItem.id);
      }),
    );
  }

  clearActiveUpscaleJob(): void {
    this.activeUpscaleJob.next(null);
    this.stopUpscalePolling();
  }

  private startUpscalePolling(mediaId: number): void {
    this.stopUpscalePolling();

    // A measured upscale runs for minutes rather than seconds, so this
    // polls on the same slow cadence as video generation rather than the
    // faster one used for images.
    this.upscalePollingSubscription = timer(5000, 15000)
      .pipe(
        switchMap(() => this.getUpscaleMediaItem(mediaId)),
        tap(latestItem => {
          this.activeUpscaleJob.next(latestItem);

          if (this.isJobFinished(latestItem)) {
            this.stopUpscalePolling();
            if (latestItem.status === JobStatus.COMPLETED) {
              handleSuccessSnackbar(
                this._snackBar,
                'Your upscaled video is ready!',
              );
            } else if (latestItem.status === JobStatus.STOPPED) {
              handleInfoSnackbar(
                this._snackBar,
                'The upscale was stopped before it finished.',
              );
            } else {
              handleErrorSnackbar(
                this._snackBar,
                {message: latestItem.errorMessage || latestItem.error_message},
                `Upscale failed: ${latestItem.errorMessage || latestItem.error_message}`,
              );
            }
          }
        }),
        catchError(err => {
          console.error('Upscale polling failed', err);
          this.stopUpscalePolling();
          return EMPTY;
        }),
      )
      .subscribe();
  }

  private stopUpscalePolling(): void {
    this.upscalePollingSubscription?.unsubscribe();
    this.upscalePollingSubscription = null;
  }

  getUpscaleMediaItem(mediaId: number): Observable<MediaItem> {
    const url = `${environment.backendURL}/gallery/item/${mediaId}`;
    return this.http.get<MediaItem>(url);
  }
}
