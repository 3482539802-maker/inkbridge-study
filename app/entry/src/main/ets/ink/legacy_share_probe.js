import dataShare from '@ohos.data.dataShare';

// JS interop checks the legacy runtime export; it does not bypass its permission check.
export async function probeLegacyDataShare(context) {
  const result = {
    schema_version: 1,
    observed_at: new Date().toISOString(),
    uri: 'datashare://com.huawei.hmos.hinote',
    api_present: typeof dataShare.createDataShareHelper === 'function',
    helper_created: false,
    code: null,
    message: '',
    private_data_read: false
  };
  try {
    if (result.api_present) {
      const helper = await dataShare.createDataShareHelper(context, result.uri);
      result.helper_created = true;
      result.message = 'Helper created; no query or write performed. URI/schema still require verification.';
      await helper.release();
    } else {
      result.message = 'Runtime does not expose createDataShareHelper to this application.';
    }
  } catch (error) {
    result.code = error.code === undefined ? null : error.code;
    result.message = error.message;
  }
  return JSON.stringify(result);
}
